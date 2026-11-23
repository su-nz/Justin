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
- 測試：`python -m pytest tests -q`（遲滯、離線門檻、清運後速率、融合、派車、覆蓋率聯集、閘門情境）
- 閘門邏輯（不需 Pi）：`python edge/vision-gate/gate.py --backend mock --scenario edge/vision-gate/scenarios/demo.json --offline --sim-dt 30`

模擬結果範例：16 桶中排入 7 站，動態路線 8.5 km vs 固定排程 11.7 km，**節省約 27%**（Paper 1 報告 30.76%）。距離目前用直線 × 1.35 近似，上線前換成 OSRM 距離矩陣。

## 正式部署

```bash
cd deploy
mosquitto_passwd -c mosquitto/passwd backend && mosquitto_passwd mosquitto/passwd NTNU-01   # 每桶一組帳號
# 放 TLS 憑證到 mosquitto/certs/
ADMIN_TOKEN=... MQTT_BACKEND_PASS=... docker compose up -d
```
- Optional: set `FULL_WEIGHT_G` to mark a bin full once its latest reported weight reaches that many grams, even when its fill level is low. Leave it unset to disable weight-based full status.
- 對外請經反向代理上 HTTPS（LINE webhook 規定）。
- MQTT ACL：裝置帳號 = bin_id，只能寫 `smartbin/<自己>/#`；後端以 topic 的 id 為準，防冒名。
- HTTP 上報（NB-IoT 節點、閘門）每桶獨立 token，建桶時只顯示一次，資料庫只存 SHA-256。

### Demo 主機（HTTP 上報、不啟動 MQTT）

Demo 主機只跑後端時，改用 [`deploy/docker-compose.demo.yml`](deploy/docker-compose.demo.yml)，依照 [`deploy/DEMO.md`](deploy/DEMO.md) 設定 `ADMIN_TOKEN` 與 `PUBLIC_URL`、建立持久化資料 volume，並設定 HTTPS 反向代理。該設定不包含 Mosquitto，且不會設定 `MQTT_HOST`。

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

> 韌體已用 PlatformIO 編譯通過（Flash 用量約 90%），但尚未在實體 XIAO ESP32C3 上執行過。

## Demo 感測節點（edge/pi-demo）

Demo 用 Pi 4 取代 ESP32：`node_pi.py` 用超音波量 `fill_pct`、HX711 量 `weight_g`，每 30 秒 `POST /api/telemetry`，LCD 顯示滿度和重量。

| 實體腳位（BCM） | 接到 |
|---|---|
| 29（GPIO5）/ 31（GPIO6） | HC-SR04 Trig / Echo（Echo 經分壓） |
| 15（GPIO22）/ 13（GPIO27） | HX711 DT / SCK |
| 17（3.3 V）/ 14（GND） | HX711 VCC / GND（**VCC 接 3.3 V，不能接 5 V**） |
| 3 / 5（I²C） | LCD1602 PCF8574（0x27） |

```bash
sudo apt install -y python3-lgpio i2c-tools
pip install --break-system-packages -r edge/pi-demo/requirements.txt
python3 edge/pi-demo/node_pi.py --tare                         # 空桶 → export HX711_OFFSET=...
HX711_OFFSET=... python3 edge/pi-demo/node_pi.py --calibrate 600   # 放 600 g → export HX711_SCALE=...
export SMARTBIN_API=http://<server>:8000 BIN_ID=NTNU-01 DEVICE_TOKEN=<token> HX711_OFFSET=... HX711_SCALE=...
python3 edge/pi-demo/node_pi.py --depth-cm 30
```
筆電測試用 `--dry`（假距離、假重量，不碰 GPIO）；沒接秤用 `--no-scale`。HX711 讀不到時該輪不送 `weight_g`，其他照常上傳。重量絕對值小於 50 g 會當作 0（空秤零點漂移，#46），用 `--zero-band-g` 或 `HX711_ZERO_BAND_G` 調整，0 = 停用。

**接近即時（#43）**：`--fast --interval 1` 每輪只取超音波 5 筆、HX711 4 筆（約 0.6 秒），LCD 和上傳約每秒一次；可以用 `--sonar-samples`、`--hx-samples` 微調。網站（調度台、市民地圖）預設每秒更新（#49），`?period=秒數` 可自訂，分頁在背景時暫停。相機模式的 `gate.py` 會限制 onnxruntime 用 `GATE_ORT_THREADS`（預設 2）個核心、並以 `GATE_NICE`（預設 10）降低優先權，避免搶走感測程式量測時序。

LCD 第二行會顯示擋板狀態（#36）：`gate.py` 開閘、拒收時寫入 `GATE_STATUS_FILE`（預設 `/tmp/smartbin-gate.json`），`node_pi.py` 每 0.5 秒讀一次，在重量後面顯示 `OPEN`（最多 15 秒）或 `REJECT`（5 秒），比 `BLOCKED` 優先。兩支程式的 `GATE_STATUS_FILE` 要設成同一個檔案；設成空字串就停用。

**開機自動啟動**：用 `edge/pi-demo/smartbin-node.service`。設定寫在 `/etc/smartbin/node.env`，token 不進 repo。LCD 位址是 0x3f 時設 `LCD_ADDR=0x3f`。
```bash
sudo useradd -r -s /usr/sbin/nologin -G gpio,i2c smartbin
sudo mkdir -p /opt/smartbin /etc/smartbin && sudo cp edge/pi-demo/node_pi.py /opt/smartbin/
sudo nano /etc/smartbin/node.env      # SMARTBIN_API=… BIN_ID=… DEVICE_TOKEN=… HX711_OFFSET=… HX711_SCALE=… DEPTH_CM=… LCD_ADDR=…
sudo chmod 600 /etc/smartbin/node.env
sudo cp edge/pi-demo/smartbin-node.service /etc/systemd/system/ && sudo systemctl enable --now smartbin-node
journalctl -u smartbin-node -f        # 每輪出現「已上傳」
```
`smartbin` 使用者要能 import `httpx`、`RPLCD`：requirements 用 `sudo pip install --break-system-packages` 裝到系統，或改 `ExecStart` 指到 venv 的 python。

## 視覺閘門（edge/vision-gate）

攝影機架在桶上方往下看，一個畫面切兩個 ROI：投入口（拒收大袋 / 小垃圾才開閘、有手不關閘）與地面（覆蓋率連續兩次 ≥5% → `ground_waste`）。平常只上傳事件和辨識結果，不上傳影像。

**即時照片（#56，預設關閉）**：調整相機角度時，可以在調度台按「開始傳即時照片」，相機模式下每 2 秒上傳一張寬 320 px、JPEG 品質 50 的照片，**5 分鐘後自動停止**。照片只存在後端記憶體（每桶最新一張，30 秒失效），不寫入硬碟或資料庫，只有 admin 看得到。照片會拍到路人，用完請按停止。

```bash
sudo apt install imx500-all python3-picamera2 python3-gpiozero
sudo cp smartbin-gate.service /etc/systemd/system/ && sudo systemctl enable --now smartbin-gate
```
GPIO：伺服 18、綠燈 23、紅燈 24、蜂鳴器 25（`Cfg` 可改）。閘門每 60 秒向後端查桶子狀態，桶滿時小垃圾也不開閘；查不到就沿用上次結果。

**劇本模式（#6）**：視覺還沒好之前，用 mock 劇本驅動實體擋板。伺服走 pigpio 的硬體計時 PWM，比較不會抖。
```bash
sudo apt install -y pigpio python3-pigpio && sudo systemctl enable --now pigpiod
python3 edge/vision-gate/gate.py --backend mock --scenario edge/vision-gate/scenarios/demo.json --drive --fps 1 --offline
```
擋板轉到位 0.6 秒後會停 PWM（detach），新版 Raspberry Pi OS 沒有 pigpio、只能用軟體 PWM 時也不會一直抖；擋板需要伺服一直出力才撐得住時加 `--servo-hold`。擋板沒開或關到位時，用 `--open-angle`／`--close-angle`（0–180°，預設 90／0；0.5–2.5 ms 對應 0–180°）對齊實物。

沒有 AI Camera 時可以用 USB 視訊鏡頭跑 `--backend onnx`（Pi 4 CPU 推論）。鏡頭不是 `/dev/video0` 時，先用 `v4l2-ctl --list-devices` 找編號，再用 `--camera 2` 或 `--camera /dev/video2` 指定（也可設環境變數 `GATE_CAMERA`）。

**模型**：預設類別 `cup, bottle, can, paper, lunchbox, garbage_bag, hand, litter`。
1. 資料：TACO 公開資料集 + 自己在師大周邊拍的垃圾袋 / 飲料杯（含夜間）
2. 訓練：`yolo detect train model=yolo11n.pt data=smartbin.yaml imgsz=640`
3. 匯出 IMX500：`yolo export model=best.pt format=imx`，把產出的 `network.rpk` 放到 `/opt/smartbin/models/`
4. 驗證用 **site-held-out split**（測試集用沒訓練過的地點），避免重蹈 Paper 3 同機位切分過度樂觀的問題。

**閘門策略與鎖定（#55）**：閘門每 3 秒向後端查桶況（原本 60 秒）；桶滿，或感測被擋（`fault`，`GATE_LOCK_BLOCKED=0` 可關閉）時鎖住，看到可投物品也不開，改用紅燈和蜂鳴器提示。調度台的「閘門策略」可以切換**自動**（相機判斷）、**強制開啟**、**強制鎖定**（有手時仍不關門），策略存在 `GATE_POLICY_FILE`（預設 `/tmp/smartbin-gate-policy`），換模式重開後也會保留。調度台會顯示閘門目前的判斷原因，例如「桶滿鎖定」「看到杯子，但不在投入口」。

**元件狀態（#53）**：調度台焦點卡片顯示每個元件的狀態。`node_pi.py` 在狀態改變或每 10 秒上報（超音波「距離太近／收不到回波」與連續輪數、秤重逾時／未校正、LCD 錯誤），`gate.py` 每 2 秒上報（模式、相機 FPS、擋板、最後辨識到的物品與座標）。**只傳辨識結果，不傳影像**，調度台用示意框畫出物品位置。

**遠端控制（#41）**：調度台焦點桶卡片的「遠端控制」可以開關擋板、測試拒收、切換劇本／相機模式、在 LCD 顯示英數字、立即量測、秤重歸零。Pi 每 2 秒主動向後端拿指令（不用開 port）。`gate.py` 要用 `run-gate.sh` 啟動，模式切換才會自動重開；相機模式啟動失敗（例如沒有模型）會自動退回劇本。
```bash
GATE_MODEL=~/yolo11n.onnx edge/vision-gate/run-gate.sh --open-angle 90 --close-angle 0   # 其他參數原樣傳給 gate.py
```
`GATE_MODE_FILE` 預設 `/tmp/smartbin-gate-mode`，重開機回到劇本模式。秤重歸零只在程式執行期間有效，要長期保留請把回報的 `HX711_OFFSET` 寫回 `node.env`。

**Demo L1（免訓練，#11）**：Pi 4 + USB 攝影機，直接用 COCO 預訓練的 YOLO11n。杯子、瓶子照常開閘；人（手）在投入口就不關閘；手提袋、背包、行李箱當垃圾袋拒收；其他物體不開閘，太大一樣拒收。
```bash
pip install ultralytics                                     # 只有匯出時需要，Pi 上不用裝
yolo export model=yolo11n.pt format=onnx imgsz=320          # 產生 yolo11n.onnx（權重不進 repo）
pip install onnxruntime opencv-python-headless              # Pi 上
python edge/vision-gate/gate.py --backend onnx --model yolo11n.onnx --labels coco --offline
```
輸入尺寸預設讀模型本身（`--imgsz` 可覆寫）。執行時每嘗試處理 20 次就印一次實際 FPS、有效影格數和平均偵測耗時。

12/1 判斷點的 ≥3 FPS 看「實際 FPS」。以最近 20 次嘗試中成功處理的影格數除以整段經過時間。讀不到畫面時只計耗時，有畫面但沒偵測到物件仍算有效影格。用 Demo 參數測量，時間包含取像、推論、閘門邏輯和每輪等待（預設 `--fps 5` 會等 200 ms）。`detect` 平均耗時供診斷使用。

## API

| 方法 | 路徑 | 權限 |
|---|---|---|
| POST | `/api/telemetry`、`/api/events` | `X-Device-Token` |
| GET | `/api/bins`、`/api/nearest?lat=&lng=` | 公開 |
| POST | `/api/bins`、`/api/dispatch/plan`、`/api/events/{id}/resolve` | `Authorization: Bearer <ADMIN_TOKEN>` |
| GET | `/api/events`、`/api/dispatch/latest`、`/api/admin/bins`（調度台用，多 `last_seen`、`open_events`、最新原始讀值 `dist_mm`、`blocked`） | admin |
| GET | `/api/bins/{id}/history?hours=1&max_points=300`（最近 N 小時 telemetry，依時間排序；`hours` ≤ 168，點數超過 `max_points` 會等距抽樣並保留最後一筆） | admin |
| POST、GET | `/api/bins/{id}/commands`（遠端控制：`gate_open`、`gate_close`、`gate_reject`、`gate_mode`、`gate_policy`、`snapshot`、`lcd_text`、`measure_now`、`tare`；60 秒沒被裝置取走就逾時） | admin |
| GET | `/api/device/commands?bin_id=&target=gate\|node`、POST `/api/device/commands/{id}/result?bin_id=`（裝置輪詢指令、回報結果） | `X-Device-Token` |
| POST | `/api/device/status`（元件狀態：`source` 為 `node` 或 `gate`，只存最新一筆，上限 8 KB，不收影像） | `X-Device-Token` |
| POST | `/api/device/snapshot?bin_id=`（即時照片，`image/jpeg` ≤ 80 KB，只存在記憶體） | `X-Device-Token` |
| GET | `/api/bins/{id}/snapshot`（最新照片，30 秒失效） | admin |
| GET | `/api/bins/{id}/status`（調度台「元件狀態」：超音波遮擋原因、秤重、LCD、閘門模式、相機 FPS、最後辨識結果） | admin |
| POST | `/line/webhook` | LINE 簽章驗證 |

## 下一步

- 距離矩陣接 OSRM；派車加時間窗（Paper 3：夜間 20:00–03:00 為傾倒高峰，清晨收）。
- 實桶量測：不同垃圾形狀下 ToF vs 實際體積誤差、電池實測曲線。
- 多點位實驗評估：路線節省、市民找桶時間（對照 Paper 2 的 18.98% / 34.7%）。
