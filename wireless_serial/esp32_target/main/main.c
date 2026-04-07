/*
 * WiLoc ESP32-C5 Target Firmware
 * WiFi SoftAP (beacon broadcaster) + BLE GATT for debug/control
 *
 * This is the device whose position we want to determine.
 * It broadcasts WiFi beacons on a fixed channel. The 4 anchor ESP32s
 * in promiscuous mode capture CSI from these beacons.
 *
 * BLE GATT provides:
 *   - Status reporting (beacon count, uptime, tx power)
 *   - Remote control (start/stop, change channel, set tx power)
 *   - Debug info (readable from phone or laptop)
 *
 * Build:
 *   idf.py --preview set-target esp32c5
 *   idf.py --preview build
 *   idf.py --preview -p /dev/tty.usbmodemXXX flash monitor
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
#include "esp_netif.h"
#include "esp_mac.h"
#include "esp_now.h"

/* NimBLE */
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "wiloc_tgt";

/* ── Configuration ── */
#define WIFI_CHANNEL         6
#define SOFTAP_SSID          "WiLoc_Target"
#define SOFTAP_PASS          ""           /* open network — no clients need to connect */
#define BEACON_INTERVAL_MS   100          /* beacon every 100ms = 10 beacons/sec */
#define BLE_DEVICE_NAME      "WiLoc_tgt"

/* BLE UUIDs — same service, different from anchors by name */
#define WILOC_SVC_UUID       0xFFE0
#define WILOC_TX_UUID        0xFFE1      /* notify: target → receiver */
#define WILOC_RX_UUID        0xFFE2      /* write: receiver → target */

/* Protocol constants */
#define SYNC_H               0xAA
#define SYNC_L               0x55
#define PKT_STATUS           0x03
#define PKT_CMD_ACK          0x04
#define PKT_HEARTBEAT        0x05
#define PKT_CMD_START        0x11
#define PKT_CMD_STOP         0x12
#define PKT_CMD_SET_CH       0x13
#define PKT_CMD_STATUS       0x14

#define FRAME_QUEUE_SIZE     16
#define HEARTBEAT_INTERVAL_MS 3000

/* ── State ── */
static uint16_t ble_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t tx_char_attr_handle;
static bool     ble_connected = false;
static bool     tx_notifications_enabled = false;
static bool     beacon_active = true;
static int      wifi_channel = WIFI_CHANNEL;
static uint32_t heartbeat_seq = 0;
static uint32_t beacon_count = 0;
static int8_t   tx_power_dbm = 20;       /* max tx power */
static QueueHandle_t frame_queue = NULL;

typedef struct {
    uint8_t data[256];
    size_t  len;
} frame_t;

/* ── CRC-8 ── */
static uint8_t crc8_table[256];

static void build_crc8_table(void) {
    for (int i = 0; i < 256; i++) {
        uint8_t crc = (uint8_t)i;
        for (int j = 0; j < 8; j++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : crc << 1;
        crc8_table[i] = crc;
    }
}

static uint8_t crc8(const uint8_t *data, size_t len) {
    uint8_t crc = 0;
    for (size_t i = 0; i < len; i++)
        crc = crc8_table[crc ^ data[i]];
    return crc;
}

static size_t encode_frame(uint8_t *buf, size_t buf_size,
                           uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    size_t total = 2 + 2 + 1 + plen + 1;
    if (total > buf_size) return 0;
    buf[0] = SYNC_H; buf[1] = SYNC_L;
    buf[2] = (plen >> 8) & 0xFF; buf[3] = plen & 0xFF;
    buf[4] = ptype;
    if (plen > 0) memcpy(&buf[5], payload, plen);
    buf[5 + plen] = crc8(&buf[4], 1 + plen);
    return total;
}

static void queue_frame(uint8_t ptype, const uint8_t *payload, uint16_t plen) {
    if (!ble_connected || !tx_notifications_enabled || !frame_queue) return;
    frame_t f;
    f.len = encode_frame(f.data, sizeof(f.data), ptype, payload, plen);
    if (f.len > 0) xQueueSend(frame_queue, &f, 0);
}

/* ── WiFi + ESP-NOW sender ──
 * ESP32-C5 CSI callback only fires for DATA frames, not management frames
 * (beacons/probes). ESP-NOW sends lightweight data frames that anchors in
 * promiscuous mode can extract CSI from — exactly how Espressif's esp-csi
 * project does it.
 */

static uint32_t espnow_send_count = 0;

static void espnow_send_cb(const esp_now_send_info_t *info, esp_now_send_status_t status) {
    if (status == ESP_NOW_SEND_SUCCESS) espnow_send_count++;
}

static void wifi_espnow_init(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Lock to 2.4GHz ch6 */
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY));
    ESP_ERROR_CHECK(esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE));

    /* Set TX power max */
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(tx_power_dbm * 4));

    /* Set bandwidth to HT20 on 2.4GHz — must match anchor CSI config */
    wifi_protocols_t protos = {
        .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N,
        .ghz_5g = 0,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protos));
    wifi_bandwidths_t bw = {
        .ghz_2g = WIFI_BW20,
        .ghz_5g = 0,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bw));

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    /* Init ESP-NOW */
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));

    /* Add broadcast peer — sends to ff:ff:ff:ff:ff:ff */
    esp_now_peer_info_t peer = {0};
    memset(peer.peer_addr, 0xFF, 6);  /* broadcast */
    peer.channel = wifi_channel;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    /* Set ESP-NOW rate to MCS0 with HT — this produces HT-LTF preamble
     * which the anchors' CSI config (acquire_csi_ht20=true) will capture. */
    esp_now_rate_config_t rate_cfg = {
        .phymode = WIFI_PHY_MODE_HT20,
        .rate = WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_cfg));

    /* Read MAC */
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);

    ESP_LOGI(TAG, "ESP-NOW sender initialized on CH%d", wifi_channel);
    ESP_LOGI(TAG, "STA MAC: %02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "TX power: %d dBm", tx_power_dbm);
}

/* Periodically send ESP-NOW broadcast packets.
 * Anchors in promiscuous mode extract CSI from these data frames. */
static void espnow_sender_task(void *arg) {
    static const uint8_t broadcast_mac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    uint8_t payload[16];  /* small payload — CSI comes from the preamble, not payload */
    uint32_t seq = 0;

    vTaskDelay(pdMS_TO_TICKS(1000));  /* let WiFi settle */
    ESP_LOGI(TAG, "ESP-NOW sender started — %d packets/sec", 1000 / BEACON_INTERVAL_MS);

    while (1) {
        if (beacon_active) {
            /* Payload: sequence number (for debugging) */
            payload[0] = (seq >> 24) & 0xFF;
            payload[1] = (seq >> 16) & 0xFF;
            payload[2] = (seq >> 8) & 0xFF;
            payload[3] = seq & 0xFF;
            seq++;

            esp_now_send(broadcast_mac, payload, sizeof(payload));
        }
        vTaskDelay(pdMS_TO_TICKS(BEACON_INTERVAL_MS));
    }
}

/* ── Command processing ── */
static void process_command(const uint8_t *data, uint16_t len) {
    if (len < 1) return;
    uint8_t cmd = data[0];
    char ack[64];

    switch (cmd) {
        case PKT_CMD_START:
            beacon_active = true;
            esp_wifi_start();
            snprintf(ack, sizeof(ack), "Beacons started");
            ESP_LOGI(TAG, "%s", ack);
            break;

        case PKT_CMD_STOP:
            beacon_active = false;
            esp_wifi_stop();
            snprintf(ack, sizeof(ack), "Beacons stopped");
            ESP_LOGI(TAG, "%s", ack);
            break;

        case PKT_CMD_SET_CH:
            if (len >= 2) {
                int ch = data[1];
                if (ch >= 1 && ch <= 13) {
                    wifi_channel = ch;
                    /* Need to restart AP to change channel */
                    esp_wifi_stop();
                    wifi_config_t cfg;
                    esp_wifi_get_config(WIFI_IF_AP, &cfg);
                    cfg.ap.channel = wifi_channel;
                    esp_wifi_set_config(WIFI_IF_AP, &cfg);
                    esp_wifi_start();
                    snprintf(ack, sizeof(ack), "Channel set to %d", ch);
                    ESP_LOGI(TAG, "%s", ack);
                }
            }
            break;

        case PKT_CMD_STATUS: {
            /* Send detailed status */
            uint8_t p[32];
            memset(p, 0, sizeof(p));
            memcpy(p, "tgt", 3);                          /* device id (bytes 0-7) */
            uint32_t uptime = (uint32_t)(esp_timer_get_time() / 1000000);
            uint32_t heap = esp_get_free_heap_size();
            p[8]  = (uptime >> 24) & 0xFF;
            p[9]  = (uptime >> 16) & 0xFF;
            p[10] = (uptime >> 8) & 0xFF;
            p[11] = uptime & 0xFF;
            p[12] = (heap >> 24) & 0xFF;
            p[13] = (heap >> 16) & 0xFF;
            p[14] = (heap >> 8) & 0xFF;
            p[15] = heap & 0xFF;
            p[16] = ble_connected ? 1 : 0;
            p[17] = (uint8_t)wifi_channel;
            queue_frame(PKT_STATUS, p, 18);
            return;
        }

        default:
            snprintf(ack, sizeof(ack), "Unknown cmd 0x%02X", cmd);
            break;
    }
    queue_frame(PKT_CMD_ACK, (uint8_t *)ack, strlen(ack));
}

/* ── BLE GATT ── */
static int gatt_access_cb(uint16_t conn_handle, uint16_t attr_handle,
                          struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        uint8_t buf[256];
        if (len > sizeof(buf)) len = sizeof(buf);
        os_mbuf_copydata(ctxt->om, 0, len, buf);

        size_t pos = 0;
        while (pos + 6 <= len) {
            if (buf[pos] != SYNC_H || buf[pos+1] != SYNC_L) { pos++; continue; }
            uint16_t plen = ((uint16_t)buf[pos+2] << 8) | buf[pos+3];
            size_t fsize = 5 + plen + 1;
            if (pos + fsize > len) break;
            if (buf[pos + 5 + plen] == crc8(&buf[pos+4], 1 + plen)) {
                uint8_t cmd_buf[256];
                cmd_buf[0] = buf[pos+4];
                if (plen > 0 && plen < sizeof(cmd_buf) - 1)
                    memcpy(&cmd_buf[1], &buf[pos+5], plen);
                process_command(cmd_buf, 1 + plen);
            }
            pos += fsize;
        }
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(WILOC_SVC_UUID),
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = BLE_UUID16_DECLARE(WILOC_TX_UUID),
                .access_cb = gatt_access_cb,
                .val_handle = &tx_char_attr_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = BLE_UUID16_DECLARE(WILOC_RX_UUID),
                .access_cb = gatt_access_cb,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            { 0 },
        },
    },
    { 0 },
};

/* ── BLE GAP ── */
static void ble_advertise(void);

static int gap_event_cb(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            if (event->connect.status == 0) {
                ble_conn_handle = event->connect.conn_handle;
                ble_connected = true;
                ble_att_set_preferred_mtu(256);
                ble_gattc_exchange_mtu(ble_conn_handle, NULL, NULL);
                ESP_LOGI(TAG, "BLE connected (handle=%d)", ble_conn_handle);
            } else {
                ble_advertise();
            }
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            ble_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            ble_connected = false;
            tx_notifications_enabled = false;
            ESP_LOGW(TAG, "BLE disconnected, re-advertising");
            ble_advertise();
            break;
        case BLE_GAP_EVENT_SUBSCRIBE:
            if (event->subscribe.attr_handle == tx_char_attr_handle)
                tx_notifications_enabled = event->subscribe.cur_notify;
            break;
        case BLE_GAP_EVENT_MTU:
            ESP_LOGI(TAG, "MTU=%d", event->mtu.value);
            break;
        default:
            break;
    }
    return 0;
}

static void ble_advertise(void) {
    struct ble_gap_adv_params adv = {0};
    struct ble_hs_adv_fields fields = {0};

    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)BLE_DEVICE_NAME;
    fields.name_len = strlen(BLE_DEVICE_NAME);
    fields.name_is_complete = 1;
    ble_uuid16_t svc = BLE_UUID16_INIT(WILOC_SVC_UUID);
    fields.uuids16 = &svc;
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;

    ble_gap_adv_set_fields(&fields);
    adv.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv.disc_mode = BLE_GAP_DISC_MODE_GEN;
    adv.itvl_min = 0x40;   /* 40ms — slower than anchors, save power */
    adv.itvl_max = 0x80;
    ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                      &adv, gap_event_cb, NULL);
    ESP_LOGI(TAG, "BLE advertising as '%s'", BLE_DEVICE_NAME);
}

static void ble_on_sync(void) {
    ble_hs_util_ensure_addr(0);
    ble_advertise();
}

static void nimble_host_task(void *p) {
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void ble_init(void) {
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = ble_on_sync;
    ble_svc_gap_device_name_set(BLE_DEVICE_NAME);
    ble_svc_gap_init();
    ble_svc_gatt_init();
    int rc = ble_gatts_count_cfg(gatt_svcs);
    assert(rc == 0);
    rc = ble_gatts_add_svcs(gatt_svcs);
    assert(rc == 0);
    nimble_port_freertos_init(nimble_host_task);
    ESP_LOGI(TAG, "BLE GATT initialized");
}

/* ── BLE sender task ── */
static void ble_sender_task(void *arg) {
    frame_t f;
    while (1) {
        if (xQueueReceive(frame_queue, &f, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (ble_connected && tx_notifications_enabled &&
                ble_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
                struct os_mbuf *om = ble_hs_mbuf_from_flat(f.data, f.len);
                if (om) ble_gatts_notify_custom(ble_conn_handle, tx_char_attr_handle, om);
            }
        }
    }
}

/* ── Heartbeat with target-specific info ── */
static void heartbeat_task(void *arg) {
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));

        uint32_t uptime = (uint32_t)(esp_timer_get_time() / 1000000);
        ESP_LOGI(TAG, "uptime=%lus heap=%lu ch=%d espnow_sent=%lu active=%s",
                 (unsigned long)uptime, (unsigned long)esp_get_free_heap_size(),
                 wifi_channel, (unsigned long)espnow_send_count,
                 beacon_active ? "yes" : "no");

        if (ble_connected && tx_notifications_enabled) {
            uint8_t hb[12];
            memset(hb, 0, 8);
            memcpy(hb, "tgt", 3);
            hb[8]  = (heartbeat_seq >> 24) & 0xFF;
            hb[9]  = (heartbeat_seq >> 16) & 0xFF;
            hb[10] = (heartbeat_seq >> 8) & 0xFF;
            hb[11] = heartbeat_seq & 0xFF;
            queue_frame(PKT_HEARTBEAT, hb, 12);
            heartbeat_seq++;
        }
    }
}

/* ── Main ── */
void app_main(void) {
    ESP_LOGI(TAG, "=== WiLoc Target (beacon broadcaster) ===");
    ESP_LOGI(TAG, "Channel: %d, Beacon interval: %dms", WIFI_CHANNEL, BEACON_INTERVAL_MS);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    build_crc8_table();
    frame_queue = xQueueCreate(FRAME_QUEUE_SIZE, sizeof(frame_t));

    wifi_espnow_init();
    ble_init();

    xTaskCreate(ble_sender_task, "ble_tx", 4096, NULL, 5, NULL);
    xTaskCreate(heartbeat_task, "hb", 2048, NULL, 3, NULL);
    xTaskCreate(espnow_sender_task, "espnow", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "Target running. Move me around to localize!");
}
