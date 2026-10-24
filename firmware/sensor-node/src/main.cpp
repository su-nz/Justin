// 夾掛式感測節點：VL53L1X 量液位 + HX711 量重量，Deep sleep，變化才上傳。
// 每次醒來：開感測器電源 → 量測（中位數濾波）→ 判斷遮擋 → 決定要不要連網 → 上傳/存入佇列 → 睡。
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <VL53L1X.h>
#include <HX711.h>
#include <time.h>
#include "config.h"

// ---------- 跨 deep sleep 保留的狀態（RTC 記憶體） ----------
struct Reading {
  uint32_t ts;
  uint16_t dist_mm;
  float fill;
  float weight_g;
  uint16_t vbat_mv;
  uint8_t blocked;
};
static const int QMAX = 24;  // 斷網時最多暫存 24 筆（2 小時）
RTC_DATA_ATTR Reading queue_[QMAX];
RTC_DATA_ATTR int qlen = 0;
RTC_DATA_ATTR float lastSentFill = -100;
RTC_DATA_ATTR uint8_t lastSentBlocked = 0;
RTC_DATA_ATTR int wakesSinceSend = 0;
RTC_DATA_ATTR int blockCount = 0;
RTC_DATA_ATTR bool timeSynced = false;

VL53L1X tof;
HX711 scale;

static int median(int *v, int n) {
  for (int i = 1; i < n; i++) {  // 插入排序，n 很小
    int k = v[i], j = i - 1;
    while (j >= 0 && v[j] > k) { v[j + 1] = v[j]; j--; }
    v[j + 1] = k;
  }
  return v[n / 2];
}

// 回傳 -1 表示量不到（全部失敗）
static int readToF() {
  digitalWrite(PIN_TOF_XSHUT, HIGH);
  delay(5);
  tof.setTimeout(500);
  if (!tof.init()) return -1;
  tof.setDistanceMode(VL53L1X::Medium);   // 最遠約 2.9 m，桶深 < 1 m 用 Medium 抗環境光較好
  tof.setMeasurementTimingBudget(50000);
  tof.setROISize(8, 8);                    // 縮小視角，避免掃到桶壁（Challenge 1：不規則形狀）
  tof.startContinuous(60);
  int v[TOF_SAMPLES], n = 0;
  for (int i = 0; i < TOF_SAMPLES + 4 && n < TOF_SAMPLES; i++) {
    tof.read();
    if (!tof.timeoutOccurred() && tof.ranging_data.range_status == VL53L1X::RangeValid)
      v[n++] = tof.ranging_data.range_mm;
  }
  tof.stopContinuous();
  digitalWrite(PIN_TOF_XSHUT, LOW);       // ToF 關機省電
  if (n < 3) return -1;
  return median(v, n);
}

static float readWeight() {
#if USE_LOADCELL
  scale.power_up();
  if (!scale.wait_ready_timeout(1000)) return NAN;
  float g = scale.get_units(HX711_SAMPLES);
  scale.power_down();
  return g;
#else
  return NAN;
#endif
}

static uint16_t readVbat() {
  uint32_t mv = 0;
  for (int i = 0; i < 8; i++) mv += analogReadMilliVolts(PIN_VBAT);
  return (mv / 8) * 2;  // 1:1 分壓
}

static float distToFill(int d) {
  float f = (float)(BIN_DEPTH_MM - d) / (BIN_DEPTH_MM - FULL_DIST_MM) * 100.0f;
  return constrain(f, 0.0f, 100.0f);
}

static bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000) delay(100);
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!timeSynced) {  // TLS 驗證憑證需要正確時間；之後 RTC 在 deep sleep 中持續計時
    configTime(8 * 3600, 0, "time.stdtime.gov.tw", "pool.ntp.org");
    struct tm tm;
    timeSynced = getLocalTime(&tm, 5000);
  }
  return true;
}

static bool publishAll(const Reading &cur) {
#if MQTT_USE_TLS
  WiFiClientSecure net;
  net.setCACert(MQTT_CA);
#else
  WiFiClient net;
#endif
  PubSubClient mqtt(net);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  if (!mqtt.connect(BIN_ID, MQTT_USER, MQTT_PASS)) return false;

  char topic[64];
  snprintf(topic, sizeof(topic), "smartbin/%s/telemetry", BIN_ID);
  auto send = [&](const Reading &r) {
    JsonDocument doc;
    if (r.ts > 1700000000) doc["ts"] = r.ts;  // 沒對時就交給後端補收到時間
    doc["fill_pct"] = roundf(r.fill * 10) / 10;
    if (r.dist_mm) doc["dist_mm"] = r.dist_mm;
    if (!isnan(r.weight_g)) doc["weight_g"] = roundf(r.weight_g);
    doc["battery_mv"] = r.vbat_mv;
    doc["rssi"] = WiFi.RSSI();
    doc["blocked"] = (bool)r.blocked;
    char buf[256];
    size_t n = serializeJson(doc, buf);
    return mqtt.publish(topic, (const uint8_t *)buf, n, false);
  };

  // 先補送斷線期間的資料（Challenge 4：不穩定網路）
  int sent = 0;
  while (sent < qlen && send(queue_[sent])) { mqtt.loop(); sent++; }
  if (sent < qlen) {  // 補送到一半又斷：保留沒送出的
    memmove(queue_, queue_ + sent, (qlen - sent) * sizeof(Reading));
    qlen -= sent;
    return false;
  }
  qlen = 0;
  bool ok = send(cur);
  mqtt.loop();
  mqtt.disconnect();
  return ok;
}

static void enqueue(const Reading &r) {
  if (qlen == QMAX) {  // 滿了丟最舊的
    memmove(queue_, queue_ + 1, (QMAX - 1) * sizeof(Reading));
    qlen--;
  }
  queue_[qlen++] = r;
}

static void sleepNow(float fill) {
  uint64_t s = (fill > BUSY_THRESHOLD) ? SLEEP_BUSY_S : SLEEP_NORMAL_S;
  digitalWrite(PIN_SENSOR_PWR, HIGH);  // P-MOSFET：HIGH = 關感測器電源
  WiFi.mode(WIFI_OFF);
  esp_sleep_enable_timer_wakeup(s * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_SENSOR_PWR, OUTPUT);
  digitalWrite(PIN_SENSOR_PWR, LOW);   // 開感測器電源
  pinMode(PIN_TOF_XSHUT, OUTPUT);
  digitalWrite(PIN_TOF_XSHUT, LOW);
  delay(10);
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
#if USE_LOADCELL
  scale.begin(PIN_HX_DOUT, PIN_HX_SCK);
  scale.set_scale(HX711_SCALE);
  scale.set_offset(HX711_OFFSET);  // 空桶時的原始讀值，安裝時量測後填入 config.h
#endif

  Reading r{};
  r.ts = (uint32_t)time(nullptr);
  int d = readToF();
  r.weight_g = readWeight();
  r.vbat_mv = readVbat();

  // Challenge 2：感測器遮擋。量不到或距離極近、且連續多次 → blocked
  bool suspicious = (d < 0) || (d < BLOCK_DIST_MM);
  blockCount = suspicious ? blockCount + 1 : 0;
  r.blocked = blockCount >= BLOCK_CONSECUTIVE;
  r.dist_mm = d > 0 ? d : 0;
  r.fill = d > 0 ? distToFill(d) : lastSentFill;  // 量不到時沿用上次值，不亂報
  if (r.fill < 0) r.fill = 0;

  // Challenge 3/5：只有「變化夠大 / 跨門檻 / 遮擋狀態改變 / 心跳」才連網
  wakesSinceSend++;
  bool crossed = (lastSentFill < 75 && r.fill >= 75) || (lastSentFill < 90 && r.fill >= 90) ||
                 (lastSentFill >= 60 && r.fill < 30);  // 清運後的大落差也要馬上報
  bool needSend = fabsf(r.fill - lastSentFill) >= REPORT_DELTA_PCT || crossed ||
                  r.blocked != lastSentBlocked || wakesSinceSend >= HEARTBEAT_WAKES;

  Serial.printf("d=%d fill=%.1f w=%.0f vbat=%u blocked=%d send=%d q=%d\n",
                d, r.fill, r.weight_g, r.vbat_mv, r.blocked, needSend, qlen);

  if (needSend) {
    if (connectWiFi() && publishAll(r)) {
      lastSentFill = r.fill;
      lastSentBlocked = r.blocked;
      wakesSinceSend = 0;
    } else {
      enqueue(r);
    }
  }
  sleepNow(r.fill);
}

void loop() {}
