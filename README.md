# Modular Smart Waste Bin & Dynamic Collection Guidance System

NTNU CSC9017 物聯網應用｜期末專題原型

把簡報的 **Modular Retrofit & Dual-Loop Architecture** 做成可跑的系統：

| 簡報模組 | 本 repo | 硬體 |
|---|---|---|
| Clip-On Sensor | `firmware/sensor-node/` | XIAO ESP32C3 + VL53L1X ToF（+ 選配 HX711 秤重） |
| Edge Vision Shutter | `edge/vision-gate/` | Pi Zero 2 W + Pi AI Camera（IMX500）+ 伺服擋板 |
| Dynamic Dispatch | `backend/app/routing.py` | 伺服器（Docker） |
| Citizen Guidance | `backend/static/index.html` + LINE webhook | 市民手機（PWA / LINE，免安裝） |

完整採購清單：`docs/硬體清單_BOM_v1.xlsx`

## 架構

```
 [夾掛節點 ESP32C3] --MQTT/TLS--> [Mosquitto] --> [FastAPI 後端 + SQLite] <--HTTP-- [視覺閘門 Pi Zero 2W]
   ToF 量液位、deep sleep                         │  狀態機(遲滯) / 滿溢預測 / 事件融合
   變化≥5% 或跨門檻才上傳                         │  OR-Tools CVRP 動態派車
                                                  ├── /static/index.html  市民地圖（找有空位的桶）
                                                  ├── /static/admin.html  清運調度台（路線、事件）
                                                  └── /line/webhook       LINE 官方帳號（傳位置→回最近空桶）
```

**Dual-loop**
- 市民迴圈：桶 ≥90% 從地圖與 LINE 回覆中移除，改導向附近有空位的桶。
- 清運迴圈：只收「已滿／預測 N 小時內會滿／有傾倒事件」的桶；將滿的順路才收；同時算出「固定排程繞全部桶」的距離當對照組。

**改進 Paper 3 的融合判斷**：攝影機看到桶邊地面有垃圾 → 後端查液位：桶 ≥90% 記為 `overflow`（該加收），未滿記為 `illegal_dumping`（違規傾倒）。

## 快速開始（不需硬體）

```bash
cd backend && pip install -r requirements.txt
uvicorn app.main:app --port 8000          # 預設 ADMIN_TOKEN=dev-admin-token
# 另一個終端
python sim/simulate.py --hours 10          # 註冊 16 個師大周邊示意桶、回填 10 小時資料、注入事件
python sim/simulate.py --hours 2 --live    # 持續推進（20 倍速）
```

- 市民地圖：http://localhost:8000/
- 調度台：http://localhost:8000/static/admin.html（token：`dev-admin-token`）→ 按「產生路線」
- 測試：`python -m pytest tests -q`（6 項：遲滯、清運後速率、融合、派車、覆蓋率聯集、閘門）
- 閘門邏輯（不需 Pi）：`python edge/vision-gate/gate.py --backend mock --scenario edge/vision-gate/scenarios/demo.json --offline --sim-dt 30`

模擬結果範例：16 桶中排入 7 站，動態路線 8.5 km vs 固定排程 11.7 km，**節省約 27%**（Paper 1 報告 30.76%）。距離目前用直線 × 1.35 近似，上線前換成 OSRM 距離矩陣。

## 正式部署

```bash
cd deploy
mosquitto_passwd -c mosquitto/passwd backend && mosquitto_passwd mosquitto/passwd NTNU-01   # 每桶一組帳號
# 放 TLS 憑證到 mosquitto/certs/
ADMIN_TOKEN=... MQTT_BACKEND_PASS=... docker compose up -d
```
- 對外請經反向代理上 HTTPS（LINE webhook 規定）。
- MQTT ACL：裝置帳號 = bin_id，只能寫 `smartbin/<自己>/#`；後端以 topic 的 id 為準，防冒名。
- HTTP 上報（NB-IoT 節點、閘門）每桶獨立 token，建桶時只顯示一次，資料庫只存 SHA-256。

## 感測節點（firmware/sensor-node）

PlatformIO 專案，板子 `seeed_xiao_esp32c3`。先改 `include/config.h`（Wi-Fi、MQTT、CA、桶深、校正值）再 `pio run -t upload`。

| XIAO 腳位 | 接到 |
|---|---|
| D4 (GPIO6) / D5 (GPIO7) | VL53L1X SDA / SCL |
| D3 (GPIO5) | VL53L1X XSHUT（量完關機） |
| D1 / D2 | HX711 DOUT / SCK（選配） |
| D0 (GPIO2) | 電池分壓 200k/200k 中點 |
| D6 (GPIO21) | AO3401 閘極 → 感測器電源開關 |
| 3V3 / GND | 感測器電源（經 MOSFET） |

對應簡報 Challenges：
1. **液位不準**：ToF 縮 ROI 避開桶壁、9 次取中位數；選配秤重融合。
2. **遮擋**：保護窗 + 連續 3 次量不到或 <25 mm 才判 `blocked`，後端顯示「維護中」、派車順路檢查。
3. **假警報**：後端狀態機遲滯 5%；變化 ≥5% 才上報。
4. **網路不穩**：RTC 記憶體佇列暫存 24 筆，連上後補送；閘門端寫 spool 檔補送。
5. **功耗**：每 5 分鐘醒（>70% 改 2 分鐘），只在需要時連 Wi-Fi，感測器斷電。粗估平均 <0.5 mA，2×18650 可撐數月以上，**需實測**。

> 本環境無法下載 ESP32 工具鏈，韌體尚未實際編譯過，第一次 build 請留意 library 版本差異。

## 視覺閘門（edge/vision-gate）

攝影機架在桶上方往下看，一個畫面切兩個 ROI：投入口（拒收大袋 / 小垃圾才開閘、有手不關閘）與地面（覆蓋率連續兩次 ≥5% → `ground_waste`）。只上傳事件，不上傳影像。

```bash
sudo apt install imx500-all python3-picamera2 python3-gpiozero
sudo cp smartbin-gate.service /etc/systemd/system/ && sudo systemctl enable --now smartbin-gate
```
GPIO：伺服 18、綠燈 23、紅燈 24（`Cfg` 可改）。

**模型**：預設類別 `cup, bottle, can, paper, lunchbox, garbage_bag, hand, litter`。
1. 資料：TACO 公開資料集 + 自己在師大周邊拍的垃圾袋 / 飲料杯（含夜間）
2. 訓練：`yolo detect train model=yolo11n.pt data=smartbin.yaml imgsz=640`
3. 匯出 IMX500：`yolo export model=best.pt format=imx`，把產出的 `network.rpk` 放到 `/opt/smartbin/models/`
4. 驗證用 **site-held-out split**（測試集用沒訓練過的地點），避免重蹈 Paper 3 同機位切分過度樂觀的問題。

## API

| 方法 | 路徑 | 權限 |
|---|---|---|
| POST | `/api/telemetry`、`/api/events` | `X-Device-Token` |
| GET | `/api/bins`、`/api/nearest?lat=&lng=` | 公開 |
| POST | `/api/bins`、`/api/dispatch/plan`、`/api/events/{id}/resolve` | `Authorization: Bearer <ADMIN_TOKEN>` |
| GET | `/api/events`、`/api/dispatch/latest` | admin |
| POST | `/line/webhook` | LINE 簽章驗證 |

## 下一步

- 距離矩陣接 OSRM；派車加時間窗（Paper 3：夜間 20:00–03:00 為傾倒高峰，清晨收）。
- 實桶量測：不同垃圾形狀下 ToF vs 實際體積誤差、電池實測曲線。
- 多點位實驗評估：路線節省、市民找桶時間（對照 Paper 2 的 18.98% / 34.7%）。
