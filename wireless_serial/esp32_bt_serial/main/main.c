/*
 * WiLoc ESP32-C5 Anchor Firmware
 * WiFi CSI capture (promiscuous mode) + BLE GATT serial transport
 *
 * Target: ESP32-C5 (BLE 5 only, no Classic BT)
 * Build with ESP-IDF v5.4+:
 *   idf.py set-target esp32c5
 *   idf.py build
 *   idf.py -p /dev/tty.usbmodemXXX flash monitor
 *
 * What this does:
 *   1. WiFi in promiscuous mode on a configurable channel — captures CSI
 *   2. BLE GATT server — sends CSI frames as notifications, receives commands
 *   3. Frame protocol: [0xAA][0x55][LEN_H][LEN_L][TYPE][PAYLOAD...][CRC8]
 *
 * BLE Service:
 *   Service UUID:  0xFFE0
 *   TX Char UUID:  0xFFE1  (notify) — ESP32 sends data to receiver
 *   RX Char UUID:  0xFFE2  (write)  — receiver sends commands to ESP32
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
#include "esp_timer.h"

/* NimBLE */
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "wiloc";

/* ── Configuration ── */
#ifndef CONFIG_WILOC_DEVICE_ID
#define CONFIG_WILOC_DEVICE_ID "anc_00"
#endif
#ifndef CONFIG_WILOC_WIFI_CHANNEL
#define CONFIG_WILOC_WIFI_CHANNEL 6
#endif

#define BLE_DEVICE_NAME  "WiLoc_" CONFIG_WILOC_DEVICE_ID
#define WIFI_CHANNEL     CONFIG_WILOC_WIFI_CHANNEL

/* BLE UUIDs */
#define WILOC_SVC_UUID   0xFFE0
#define WILOC_TX_UUID    0xFFE1  /* notify: ESP32 → receiver */
#define WILOC_RX_UUID    0xFFE2  /* write:  receiver → ESP32 */

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

/* BLE MTU — request large MTU for efficient CSI transfer */
#define PREFERRED_MTU    512

/* ── State ── */
static uint16_t ble_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t tx_char_attr_handle;
static bool     ble_connected = false;
static bool     tx_notifications_enabled = false;
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
}

static uint8_t crc8(const uint8_t *data, size_t len) {
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; i++)
        crc = crc8_table[crc ^ data[i]];
    return crc;
}

/* ── Frame encoding ── */
static size_t encode_frame(uint8_t *buf, size_t buf_size,
                           uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    size_t total = 2 + 2 + 1 + plen + 1;
    if (total > buf_size) return 0;

    buf[0] = SYNC_H;
    buf[1] = SYNC_L;
    buf[2] = (plen >> 8) & 0xFF;
    buf[3] = plen & 0xFF;
    buf[4] = ptype;
    if (plen > 0) memcpy(&buf[5], payload, plen);
    buf[5 + plen] = crc8(&buf[4], 1 + plen);

    return total;
}

/* Queue a frame for the BLE sender task */
static void queue_frame(uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    if (!ble_connected || !tx_notifications_enabled || frame_queue == NULL) return;

    frame_t f;
    f.len = encode_frame(f.data, sizeof(f.data), ptype, payload, plen);
    if (f.len > 0) {
        xQueueSend(frame_queue, &f, 0);
    }
}

/* ── CSI callback ── */
static uint32_t csi_total_count = 0;
static uint32_t csi_debug_timer = 0;

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info) {
    /* Debug: log ALL CSI callbacks regardless of BLE state */
    csi_total_count++;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (now_ms - csi_debug_timer > 3000) {  /* every 3s */
        csi_debug_timer = now_ms;
        if (info && info->mac) {
            ESP_LOGI(TAG, "CSI debug: %lu total, last_mac=%02x:%02x:%02x:%02x:%02x:%02x len=%d vld=%d",
                     (unsigned long)csi_total_count,
                     info->mac[0], info->mac[1], info->mac[2],
                     info->mac[3], info->mac[4], info->mac[5],
                     info ? info->len : 0,
                     info ? info->rx_ctrl.rx_channel_estimate_info_vld : -1);
        }
    }

    if (!csi_capture_active || !ble_connected || !tx_notifications_enabled) return;
    if (info == NULL || info->buf == NULL) return;

    uint32_t ts = (uint32_t)(esp_timer_get_time() / 1000);

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
    memcpy(&payload[off], CONFIG_WILOC_DEVICE_ID, strlen(CONFIG_WILOC_DEVICE_ID) < 8 ? strlen(CONFIG_WILOC_DEVICE_ID) : 8);
    off += 8;

    /* target_mac (6 bytes) */
    memcpy(&payload[off], info->mac, 6);
    off += 6;

    /* rssi (int8) */
    payload[off++] = (uint8_t)(int8_t)info->rx_ctrl.rssi;

    /* channel (uint8) */
    payload[off++] = (uint8_t)info->rx_ctrl.channel;

    /* bandwidth (uint8): use secondary channel as indicator (0=20MHz, else 40MHz) */
    payload[off++] = (uint8_t)(info->rx_ctrl.second != 0 ? 1 : 0);

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

    /* Lock to 2.4GHz — our target AP is on 2.4GHz ch6 */
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));

    ESP_ERROR_CHECK(esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /* ESP32-C5 CSI config — matches Espressif's esp-csi receiver example.
     * Key: legacy=off, ht20/ht40=on, HE modes=off for ESP-NOW data frames. */
    wifi_csi_config_t csi_cfg = {0};
    csi_cfg.enable = 1;
    csi_cfg.acquire_csi_legacy = 0;
    csi_cfg.acquire_csi_force_lltf = 0;
    csi_cfg.acquire_csi_ht20 = 1;
    csi_cfg.acquire_csi_ht40 = 1;
    csi_cfg.acquire_csi_vht = 0;
    csi_cfg.acquire_csi_su = 0;
    csi_cfg.acquire_csi_mu = 0;
    csi_cfg.acquire_csi_dcm = 0;
    csi_cfg.acquire_csi_beamformed = 0;
    csi_cfg.acquire_csi_he_stbc_mode = 2;
    csi_cfg.val_scale_cfg = 0;
    csi_cfg.dump_ack_en = 0;
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "WiFi promiscuous mode on channel %d, CSI enabled", wifi_channel);
}

/* ── Process incoming command ── */
static void process_command(const uint8_t *data, uint16_t len) {
    if (len < 1) return;
    uint8_t cmd_type = data[0];
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

        case PKT_CMD_SET_CH:
            if (len >= 2) {
                int new_ch = data[1];
                if (new_ch >= 1 && new_ch <= 13) {
                    wifi_channel = new_ch;
                    esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE);
                    snprintf(ack_msg, sizeof(ack_msg), "Channel set to %d", new_ch);
                    ESP_LOGI(TAG, "%s", ack_msg);
                }
            }
            break;

        case PKT_CMD_STATUS: {
            uint8_t status_payload[18];
            memset(status_payload, 0, 8);
            memcpy(status_payload, CONFIG_WILOC_DEVICE_ID, strlen(CONFIG_WILOC_DEVICE_ID) < 8 ? strlen(CONFIG_WILOC_DEVICE_ID) : 8);
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
            status_payload[16] = ble_connected ? 1 : 0;
            status_payload[17] = (uint8_t)wifi_channel;
            queue_frame(PKT_STATUS, status_payload, 18);
            return;
        }

        default:
            snprintf(ack_msg, sizeof(ack_msg), "Unknown command 0x%02X", cmd_type);
            ESP_LOGW(TAG, "%s", ack_msg);
            break;
    }

    queue_frame(PKT_CMD_ACK, (uint8_t *)ack_msg, strlen(ack_msg));
}

/* ── BLE GATT access callback ── */
static int wiloc_gatt_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                                 struct ble_gatt_access_ctxt *ctxt, void *arg) {
    /* RX characteristic — receiver writes commands here */
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        /* Parse frames from incoming BLE write */
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        uint8_t buf[256];
        if (len > sizeof(buf)) len = sizeof(buf);
        os_mbuf_copydata(ctxt->om, 0, len, buf);

        /* Decode frame(s) in the write */
        size_t pos = 0;
        while (pos + 6 <= len) {
            if (buf[pos] != SYNC_H || buf[pos+1] != SYNC_L) {
                pos++;
                continue;
            }
            uint16_t plen = ((uint16_t)buf[pos+2] << 8) | buf[pos+3];
            size_t frame_size = 5 + plen + 1;
            if (pos + frame_size > len) break;

            uint8_t expected_crc = crc8(&buf[pos+4], 1 + plen);
            if (buf[pos + 5 + plen] == expected_crc) {
                uint8_t ptype = buf[pos+4];
                uint8_t cmd_buf[256];
                cmd_buf[0] = ptype;
                if (plen > 0 && plen < sizeof(cmd_buf) - 1)
                    memcpy(&cmd_buf[1], &buf[pos+5], plen);
                process_command(cmd_buf, 1 + plen);
            }
            pos += frame_size;
        }
        return 0;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

/* ── GATT service definition ── */
static const struct ble_gatt_svc_def wiloc_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(WILOC_SVC_UUID),
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                /* TX: ESP32 → receiver (notify) */
                .uuid = BLE_UUID16_DECLARE(WILOC_TX_UUID),
                .access_cb = wiloc_gatt_access_cb,
                .val_handle = &tx_char_attr_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                /* RX: receiver → ESP32 (write) */
                .uuid = BLE_UUID16_DECLARE(WILOC_RX_UUID),
                .access_cb = wiloc_gatt_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            { 0 }, /* sentinel */
        },
    },
    { 0 }, /* sentinel */
};

/* ── BLE GAP event handler ── */
static void ble_advertise(void);

static int ble_gap_event_cb(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            if (event->connect.status == 0) {
                ble_conn_handle = event->connect.conn_handle;
                ble_connected = true;
                ESP_LOGI(TAG, "BLE client connected (handle=%d)", ble_conn_handle);

                /* Request larger MTU for efficient CSI transfer */
                ble_att_set_preferred_mtu(PREFERRED_MTU);
                ble_gattc_exchange_mtu(ble_conn_handle, NULL, NULL);

                /* Request 2M PHY for higher throughput (BLE 5) */
                ble_gap_set_prefered_le_phy(ble_conn_handle,
                    BLE_GAP_LE_PHY_2M_MASK, BLE_GAP_LE_PHY_2M_MASK,
                    BLE_GAP_LE_PHY_CODED_ANY);
            } else {
                ble_advertise();
            }
            break;

        case BLE_GAP_EVENT_DISCONNECT:
            ble_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            ble_connected = false;
            tx_notifications_enabled = false;
            ESP_LOGW(TAG, "BLE client disconnected, re-advertising");
            ble_advertise();
            break;

        case BLE_GAP_EVENT_SUBSCRIBE:
            if (event->subscribe.attr_handle == tx_char_attr_handle) {
                tx_notifications_enabled = event->subscribe.cur_notify;
                ESP_LOGI(TAG, "TX notifications %s",
                         tx_notifications_enabled ? "enabled" : "disabled");
            }
            break;

        case BLE_GAP_EVENT_MTU:
            ESP_LOGI(TAG, "MTU updated to %d", event->mtu.value);
            break;

        default:
            break;
    }
    return 0;
}

/* ── BLE advertising ── */
static void ble_advertise(void) {
    struct ble_gap_adv_params adv_params = {0};
    struct ble_hs_adv_fields fields = {0};

    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)BLE_DEVICE_NAME;
    fields.name_len = strlen(BLE_DEVICE_NAME);
    fields.name_is_complete = 1;

    /* Include service UUID in advertisement */
    ble_uuid16_t svc_uuid = BLE_UUID16_INIT(WILOC_SVC_UUID);
    fields.uuids16 = &svc_uuid;
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;

    ble_gap_adv_set_fields(&fields);

    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    adv_params.itvl_min = 0x20;  /* 20ms */
    adv_params.itvl_max = 0x40;  /* 40ms */

    ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                      &adv_params, ble_gap_event_cb, NULL);

    ESP_LOGI(TAG, "BLE advertising as '%s'", BLE_DEVICE_NAME);
}

/* ── NimBLE host sync callback ── */
static void ble_on_sync(void) {
    /* Make sure we have a proper address */
    ble_hs_util_ensure_addr(0);

    ble_advertise();
}

static void ble_on_reset(int reason) {
    ESP_LOGW(TAG, "BLE host reset, reason=%d", reason);
}

/* ── NimBLE host task ── */
static void nimble_host_task(void *param) {
    nimble_port_run();
    nimble_port_freertos_deinit();
}

/* ── BLE init ── */
static void ble_init(void) {
    ESP_ERROR_CHECK(nimble_port_init());

    ble_hs_cfg.sync_cb = ble_on_sync;
    ble_hs_cfg.reset_cb = ble_on_reset;

    /* Set device name */
    ble_svc_gap_device_name_set(BLE_DEVICE_NAME);

    /* Initialize GATT services */
    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(wiloc_gatt_svcs);
    assert(rc == 0);
    rc = ble_gatts_add_svcs(wiloc_gatt_svcs);
    assert(rc == 0);

    /* Start NimBLE host task */
    nimble_port_freertos_init(nimble_host_task);

    ESP_LOGI(TAG, "BLE GATT initialized as '%s'", BLE_DEVICE_NAME);
}

/* ── BLE sender task: drains frame queue and sends as notifications ── */
static void ble_sender_task(void *arg) {
    frame_t f;
    while (1) {
        if (xQueueReceive(frame_queue, &f, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (ble_connected && tx_notifications_enabled &&
                ble_conn_handle != BLE_HS_CONN_HANDLE_NONE) {

                struct os_mbuf *om = ble_hs_mbuf_from_flat(f.data, f.len);
                if (om) {
                    int rc = ble_gatts_notify_custom(ble_conn_handle,
                                                     tx_char_attr_handle, om);
                    if (rc != 0 && rc != BLE_HS_ENOMEM) {
                        ESP_LOGW(TAG, "Notify failed: %d", rc);
                    }
                    /* If ENOMEM, just drop — BLE congested, CSI packet loss OK */
                }
            }
        }
    }
}

/* ── Heartbeat task ── */
static void heartbeat_task(void *arg) {
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
        if (ble_connected && tx_notifications_enabled) {
            uint8_t hb_payload[12];
            memset(hb_payload, 0, 8);
            memcpy(hb_payload, CONFIG_WILOC_DEVICE_ID, strlen(CONFIG_WILOC_DEVICE_ID) < 8 ? strlen(CONFIG_WILOC_DEVICE_ID) : 8);
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
    ESP_LOGI(TAG, "WiLoc Anchor: %s (ESP32-C5, BLE)", CONFIG_WILOC_DEVICE_ID);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    build_crc8_table();
    frame_queue = xQueueCreate(FRAME_QUEUE_SIZE, sizeof(frame_t));

    /* Init subsystems */
    wifi_init();
    ble_init();

    /* Start tasks — ESP32-C5 is single-core, so no core pinning */
    xTaskCreate(ble_sender_task, "ble_sender", 4096, NULL, 5, NULL);
    xTaskCreate(heartbeat_task, "heartbeat", 2048, NULL, 3, NULL);

    ESP_LOGI(TAG, "Anchor running. WiFi CH%d, BLE name '%s'", wifi_channel, BLE_DEVICE_NAME);
}
