#pragma once
// ===== 每顆節點要改的設定 =====
#define BIN_ID            "NTNU-01"
#define WIFI_SSID         "your-ssid"
#define WIFI_PASS         "your-pass"
#define MQTT_HOST         "mqtt.example.org"
#define MQTT_PORT         8883           // TLS；實驗室內網測試可改 1883 並把 MQTT_USE_TLS 設 0
#define MQTT_USE_TLS      1
#define MQTT_USER         BIN_ID         // broker ACL：此帳號只能寫 smartbin/<BIN_ID>/#
#define MQTT_PASS         "device-secret"

// broker 的 CA（PEM）。Let's Encrypt 用 ISRG Root X1。
static const char *MQTT_CA = R"PEM(
-----BEGIN CERTIFICATE-----
PASTE_CA_HERE
-----END CERTIFICATE-----
)PEM";

// ===== 桶子幾何（安裝後量測） =====
#define BIN_DEPTH_MM      700   // 感測器到桶底距離
#define FULL_DIST_MM      50    // 感測器到「滿」的位置（口緣下方）距離
#define BLOCK_DIST_MM     25    // 小於此距離視為感測窗被蓋住
#define BLOCK_CONSECUTIVE 3     // 連續 N 次才判定遮擋（避免手伸進去造成誤判）

// ===== 量測與省電策略 =====
#define SLEEP_NORMAL_S    300   // 一般 5 分鐘醒一次
#define SLEEP_BUSY_S      120   // 滿度 > 70% 改 2 分鐘
#define BUSY_THRESHOLD    70
#define REPORT_DELTA_PCT  5     // 變化 ≥ 5% 才上傳（省電：連線是最耗電的動作）
#define HEARTBEAT_WAKES   6     // 沒變化也至少每 6 次醒來（≈30 分）送一次心跳
#define TOF_SAMPLES       9     // 取中位數
#define HX711_SAMPLES     10
#define HX711_SCALE       420.0f   // 放已知重量（如 1 kg 水）後：(raw - offset) / 1000
#define HX711_OFFSET      0L       // 空桶（含內袋）時的原始讀值 scale.read_average(20)
#define USE_LOADCELL      1        // 沒裝秤重模組就設 0

// ===== 腳位（XIAO ESP32C3） =====
#define PIN_SDA           6   // D4
#define PIN_SCL           7   // D5
#define PIN_TOF_XSHUT     5   // D3：拉低 → ToF 關機
#define PIN_HX_DOUT       3   // D1
#define PIN_HX_SCK        4   // D2
#define PIN_VBAT          2   // D0：1:1 分壓（2×200k）量電池
#define PIN_SENSOR_PWR    21  // D6：P-MOSFET 控制感測器電源（可選）
