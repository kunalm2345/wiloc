/*
 * WiLoc ESP32 Anchor Firmware
 * WiFi CSI capture (promiscuous mode) + Bluetooth SPP serial transport
 *
 * Build with ESP-IDF v5.x:
 *   idf.py set-target esp32
 *   idf.py menuconfig   # enable Bluetooth -> Classic BT -> SPP
 *   idf.py build
 *   idf.py -p /dev/ttyUSB0 flash monitor
 *
 * What this does:
 *   1. WiFi in promiscuous mode on a configurable channel — captures CSI
 *   2. Bluetooth SPP server — sends CSI frames to Orin Nano, receives commands
 *   3. Frame protocol: [0xAA][0x55][LEN_H][LEN_L][TYPE][PAYLOAD...][CRC8]
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_spp_api.h"
#include "esp_timer.h"

static const char *TAG = "wiloc";

/* ── Configuration ── */
#ifndef CONFIG_WILOC_DEVICE_ID
#define CONFIG_WILOC_DEVICE_ID "anchor_00"
#endif
#ifndef CONFIG_WILOC_WIFI_CHANNEL
#define CONFIG_WILOC_WIFI_CHANNEL 6
#endif

#define BT_DEVICE_NAME   "WiLoc_" CONFIG_WILOC_DEVICE_ID
#define SPP_SERVER_NAME  "WiLoc_SPP"
#define WIFI_CHANNEL     CONFIG_WILOC_WIFI_CHANNEL

/* ── Protocol constants (must match protocol.py) ── */
#define SYNC_H           0xAA
#define SYNC_L           0x55
#define PKT_CSI_DATA     0x01
#define PKT_RSSI_DATA    0x02
#define PKT_STATUS       0x03
#define PKT_CMD_ACK      0x04
#define PKT_HEARTBEAT    0x05
#define PKT_CMD          0x10
#define PKT_CMD_START    0x11
#define PKT_CMD_STOP     0x12
#define PKT_CMD_SET_CH   0x13
#define PKT_CMD_STATUS   0x14

#define MAX_CSI_LEN      384
#define FRAME_QUEUE_SIZE 32
#define HEARTBEAT_INTERVAL_MS 5000

/* ── State ── */
static uint32_t spp_handle = 0;
static bool     bt_connected = false;
static bool     csi_capture_active = true;
static int      wifi_channel = WIFI_CHANNEL;
static uint32_t heartbeat_seq = 0;
static QueueHandle_t frame_queue = NULL;

/* Frame buffer for queuing */
typedef struct {
    uint8_t data[512];
    size_t  len;
} frame_t;

/* ── CRC-8 (MAXIM, polynomial 0x31) ── */
static uint8_t crc8_table[256];
static bool crc8_table_built = false;

static void build_crc8_table(void) {
    for (int i = 0; i < 256; i++) {
        uint8_t crc = (uint8_t)i;
        for (int j = 0; j < 8; j++) {
            if (crc & 0x80)
                crc = (crc << 1) ^ 0x31;
            else
                crc = crc << 1;
        }
        crc8_table[i] = crc;
    }
    crc8_table_built = true;
}

static uint8_t crc8(const uint8_t *data, size_t len) {
    if (!crc8_table_built) build_crc8_table();
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; i++)
        crc = crc8_table[crc ^ data[i]];
    return crc;
}

/* ── Frame encoding ── */
static size_t encode_frame(uint8_t *buf, size_t buf_size,
                           uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    size_t total = 2 + 2 + 1 + plen + 1; /* sync + len + type + payload + crc */
    if (total > buf_size) return 0;

    buf[0] = SYNC_H;
    buf[1] = SYNC_L;
    buf[2] = (plen >> 8) & 0xFF;
    buf[3] = plen & 0xFF;
    buf[4] = ptype;
    if (plen > 0) memcpy(&buf[5], payload, plen);

    /* CRC over type + payload */
    buf[5 + plen] = crc8(&buf[4], 1 + plen);

    return total;
}

/* Queue a frame for the BT sender task */
static void queue_frame(uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    if (!bt_connected || frame_queue == NULL) return;

    frame_t f;
    f.len = encode_frame(f.data, sizeof(f.data), ptype, payload, plen);
    if (f.len > 0) {
        /* Non-blocking: drop frame if queue full (CSI is bursty) */
        xQueueSend(frame_queue, &f, 0);
    }
}

/* ── CSI callback ── */
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info) {
    if (!csi_capture_active || !bt_connected) return;
    if (info == NULL || info->buf == NULL) return;

    uint32_t ts = (uint32_t)(esp_timer_get_time() / 1000); /* ms */

    /* Build CSI_DATA payload matching protocol.py format:
     *   timestamp_ms : uint32  (4)
     *   anchor_id    : 8 bytes
     *   target_mac   : 6 bytes
     *   rssi         : int8    (1)
     *   channel      : uint8   (1)
     *   bandwidth    : uint8   (1)
     *   csi_len      : uint16  (2)
     *   csi_data     : csi_len bytes
     */
    uint16_t csi_len = info->len;
    if (csi_len > MAX_CSI_LEN) csi_len = MAX_CSI_LEN;
    uint16_t payload_len = 4 + 8 + 6 + 1 + 1 + 1 + 2 + csi_len;

    uint8_t payload[512];
    if (payload_len > sizeof(payload)) return;

    size_t off = 0;

    /* timestamp_ms (big-endian) */
    payload[off++] = (ts >> 24) & 0xFF;
    payload[off++] = (ts >> 16) & 0xFF;
    payload[off++] = (ts >> 8) & 0xFF;
    payload[off++] = ts & 0xFF;

    /* anchor_id (8 bytes, zero-padded) */
    memset(&payload[off], 0, 8);
    strncpy((char *)&payload[off], CONFIG_WILOC_DEVICE_ID, 8);
    off += 8;

    /* target_mac (6 bytes) */
    memcpy(&payload[off], info->mac, 6);
    off += 6;

    /* rssi (int8) */
    payload[off++] = (uint8_t)(int8_t)info->rx_ctrl.rssi;

    /* channel (uint8) */
    payload[off++] = (uint8_t)info->rx_ctrl.channel;

    /* bandwidth (uint8): 0=20MHz, 1=40MHz */
    payload[off++] = (uint8_t)info->rx_ctrl.cwb;

    /* csi_len (big-endian uint16) */
    payload[off++] = (csi_len >> 8) & 0xFF;
    payload[off++] = csi_len & 0xFF;

    /* csi_data */
    memcpy(&payload[off], info->buf, csi_len);
    off += csi_len;

    queue_frame(PKT_CSI_DATA, payload, (uint16_t)off);
}

/* ── WiFi init (promiscuous mode for CSI) ── */
static void wifi_init(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_NULL));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Set channel */
    ESP_ERROR_CHECK(esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE));

    /* Enable promiscuous mode */
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /* Register CSI callback */
    wifi_csi_config_t csi_cfg = {
        .lltf_en = true,
        .htltf_en = true,
        .stbc_htltf2_en = true,
        .ltf_merge_en = true,
        .channel_filter_en = false,
        .manu_scale = false,
        .shift = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "WiFi promiscuous mode on channel %d, CSI enabled", wifi_channel);
}

/* ── Process incoming command from Orin Nano ── */
static void process_command(const uint8_t *payload, uint16_t len) {
    if (len < 1) return;
    uint8_t cmd_type = payload[0];

    /* For framed commands, the first byte is the packet type already decoded.
     * But we also support raw CMD packets with text payload. */
    char ack_msg[64];

    switch (cmd_type) {
        case PKT_CMD_START:
            csi_capture_active = true;
            snprintf(ack_msg, sizeof(ack_msg), "CSI capture started");
            ESP_LOGI(TAG, "%s", ack_msg);
            break;

        case PKT_CMD_STOP:
            csi_capture_active = false;
            snprintf(ack_msg, sizeof(ack_msg), "CSI capture stopped");
            ESP_LOGI(TAG, "%s", ack_msg);
            break;

        case PKT_CMD_SET_CH: {
            if (len >= 2) {
                int new_ch = payload[1];
                if (new_ch >= 1 && new_ch <= 13) {
                    wifi_channel = new_ch;
                    esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE);
                    snprintf(ack_msg, sizeof(ack_msg), "Channel set to %d", new_ch);
                    ESP_LOGI(TAG, "%s", ack_msg);
                }
            }
            break;
        }

        case PKT_CMD_STATUS: {
            /* Send status packet */
            uint8_t status_payload[18];
            memset(status_payload, 0, 8);
            strncpy((char *)status_payload, CONFIG_WILOC_DEVICE_ID, 8);
            uint32_t uptime = (uint32_t)(esp_timer_get_time() / 1000000);
            uint32_t heap = esp_get_free_heap_size();
            status_payload[8]  = (uptime >> 24) & 0xFF;
            status_payload[9]  = (uptime >> 16) & 0xFF;
            status_payload[10] = (uptime >> 8) & 0xFF;
            status_payload[11] = uptime & 0xFF;
            status_payload[12] = (heap >> 24) & 0xFF;
            status_payload[13] = (heap >> 16) & 0xFF;
            status_payload[14] = (heap >> 8) & 0xFF;
            status_payload[15] = heap & 0xFF;
            status_payload[16] = bt_connected ? 1 : 0;
            status_payload[17] = (uint8_t)wifi_channel;
            queue_frame(PKT_STATUS, status_payload, 18);
            return; /* don't send ACK for status request */
        }

        default:
            snprintf(ack_msg, sizeof(ack_msg), "Unknown command 0x%02X", cmd_type);
            ESP_LOGW(TAG, "%s", ack_msg);
            break;
    }

    /* Send ACK */
    queue_frame(PKT_CMD_ACK, (uint8_t *)ack_msg, strlen(ack_msg));
}

/* ── BT SPP callback ── */
static void spp_cb(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
    switch (event) {
        case ESP_SPP_INIT_EVT:
            ESP_LOGI(TAG, "SPP initialized");
            esp_spp_start_srv(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_SLAVE, 0, SPP_SERVER_NAME);
            break;

        case ESP_SPP_START_EVT:
            ESP_LOGI(TAG, "SPP server started, discoverable");
            esp_bt_gap_set_device_name(BT_DEVICE_NAME);
            esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
            break;

        case ESP_SPP_SRV_OPEN_EVT:
            spp_handle = param->srv_open.handle;
            bt_connected = true;
            ESP_LOGI(TAG, "BT client connected! handle=%lu", (unsigned long)spp_handle);
            break;

        case ESP_SPP_DATA_IND_EVT: {
            /* Incoming data from Orin Nano — parse frames */
            static uint8_t rx_buf[1024];
            static size_t rx_len = 0;

            size_t incoming = param->data_ind.len;
            if (rx_len + incoming > sizeof(rx_buf)) rx_len = 0; /* overflow: reset */
            memcpy(&rx_buf[rx_len], param->data_ind.data, incoming);
            rx_len += incoming;

            /* Try to decode frames */
            size_t pos = 0;
            while (pos + 6 <= rx_len) {
                if (rx_buf[pos] != SYNC_H || rx_buf[pos+1] != SYNC_L) {
                    pos++;
                    continue;
                }
                uint16_t plen = ((uint16_t)rx_buf[pos+2] << 8) | rx_buf[pos+3];
                size_t frame_size = 5 + plen + 1;
                if (pos + frame_size > rx_len) break; /* incomplete */

                uint8_t expected_crc = crc8(&rx_buf[pos+4], 1 + plen);
                if (rx_buf[pos + 5 + plen] == expected_crc) {
                    uint8_t ptype = rx_buf[pos+4];
                    /* Build a small buffer with [ptype][payload] for process_command */
                    uint8_t cmd_buf[256];
                    cmd_buf[0] = ptype;
                    if (plen > 0 && plen < sizeof(cmd_buf) - 1)
                        memcpy(&cmd_buf[1], &rx_buf[pos+5], plen);
                    process_command(cmd_buf, 1 + plen);
                }
                pos += frame_size;
            }

            /* Shift remaining data to front */
            if (pos > 0) {
                rx_len -= pos;
                memmove(rx_buf, &rx_buf[pos], rx_len);
            }
            break;
        }

        case ESP_SPP_CLOSE_EVT:
            bt_connected = false;
            spp_handle = 0;
            ESP_LOGW(TAG, "BT client disconnected");
            /* Re-enable discoverability */
            esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE);
            break;

        default:
            break;
    }
}

/* ── Bluetooth init ── */
static void bt_init(void) {
    /* Release BLE memory (we only use Classic BT) */
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));

    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    bt_cfg.mode = ESP_BT_MODE_CLASSIC_BT;
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    ESP_ERROR_CHECK(esp_spp_register_callback(spp_cb));

    esp_spp_cfg_t spp_cfg = {
        .mode = ESP_SPP_MODE_CB,
        .enable_l2cap_ertm = false,
    };
    ESP_ERROR_CHECK(esp_spp_enhanced_init(&spp_cfg));

    ESP_LOGI(TAG, "Bluetooth SPP initialized as '%s'", BT_DEVICE_NAME);
}

/* ── BT sender task: drains frame queue and sends over SPP ── */
static void bt_sender_task(void *arg) {
    frame_t f;
    while (1) {
        if (xQueueReceive(frame_queue, &f, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (bt_connected && spp_handle != 0) {
                esp_spp_write(spp_handle, f.len, f.data);
            }
        }
    }
}

/* ── Heartbeat task ── */
static void heartbeat_task(void *arg) {
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
        if (bt_connected) {
            uint8_t hb_payload[12];
            memset(hb_payload, 0, 8);
            strncpy((char *)hb_payload, CONFIG_WILOC_DEVICE_ID, 8);
            hb_payload[8]  = (heartbeat_seq >> 24) & 0xFF;
            hb_payload[9]  = (heartbeat_seq >> 16) & 0xFF;
            hb_payload[10] = (heartbeat_seq >> 8) & 0xFF;
            hb_payload[11] = heartbeat_seq & 0xFF;
            queue_frame(PKT_HEARTBEAT, hb_payload, 12);
            heartbeat_seq++;
        }
    }
}

/* ── Main ── */
void app_main(void) {
    ESP_LOGI(TAG, "WiLoc Anchor: %s", CONFIG_WILOC_DEVICE_ID);

    /* NVS init (required for WiFi + BT) */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    build_crc8_table();
    frame_queue = xQueueCreate(FRAME_QUEUE_SIZE, sizeof(frame_t));

    /* Init subsystems */
    wifi_init();
    bt_init();

    /* Start tasks */
    xTaskCreatePinnedToCore(bt_sender_task, "bt_sender", 4096, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(heartbeat_task, "heartbeat", 2048, NULL, 3, NULL, 1);

    ESP_LOGI(TAG, "Anchor running. WiFi CH%d, BT name '%s'", wifi_channel, BT_DEVICE_NAME);
}
