# Agent 指南

接手這個 repo 的 coding agent 先讀這份，再讀指派的 issue 和總覽 issue #1。協作流程（分支、commit、PR）照 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 專案

NTNU CSC9017 物聯網期末專題：智慧垃圾桶。Demo 版是 1 台 Raspberry Pi 4 接超音波、LCD、LED、蜂鳴器、PIR、SG90 擋板和相機，資料上傳到 FastAPI 後端，市民用地圖和 LINE 找有空位的桶。

| 路徑 | 內容 |
|---|---|
| `backend/app/` | FastAPI 後端：`main.py`（API）、`logic.py`（滿度分級、融合判斷）、`routing.py`（派車）、`line_bot.py` |
| `backend/static/` | 市民地圖 `index.html`、調度台 `admin.html` |
| `edge/pi-demo/` | Demo 版感測程式 `node_pi.py`（見 #5） |
| `edge/vision-gate/` | 視覺閘門 `gate.py`、劇本 `scenarios/`、systemd 服務 |
| `firmware/sensor-node/` | 正式版 ESP32 韌體（Demo 不用） |
| `sim/simulate.py` | 模擬多個桶與事件，不需硬體 |
| `tests/` | 單元測試 |
| `deploy/` | Docker Compose、Mosquitto 設定 |

## 常用指令

```bash
cd backend && pip install -r requirements.txt
uvicorn app.main:app --port 8000                 # 本機後端，ADMIN_TOKEN 預設 dev-admin-token
python sim/simulate.py --hours 10                # 另一個終端：灌模擬資料
python -m pytest tests -q                        # 在 repo 根目錄跑測試
python edge/vision-gate/gate.py --backend mock --scenario edge/vision-gate/scenarios/demo.json --offline --sim-dt 30
```

## 規則

- **設定用環境變數**：`SMARTBIN_API`、`BIN_ID`、`DEVICE_TOKEN`；後端另有 `ADMIN_TOKEN`、`PUBLIC_URL`、`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`。不要把 token 寫死在程式、測試或 commit 裡。
- **GPIO 分配**（同一台 Pi 4，不要互相搶）：`node_pi.py` 用 GPIO5（Trig）、GPIO6（Echo）、I²C LCD（0x27）；`gate.py` 用 GPIO18（伺服）、23/24（LED）、25（蜂鳴器）；GPIO17（PIR）保留。
- **API 介面**：感測 `POST /api/telemetry`、閘門 `POST /api/events`，都帶 header `X-Device-Token`。改欄位要同步改 README、issue #1 和呼叫端。
- **能在筆電驗證就先驗證**：硬體相關程式要提供 `--dry` 或 mock 模式，讓沒有 Pi 也能跑。
- **測試**：改到 `backend/` 或 `edge/vision-gate/` 的邏輯，`python -m pytest tests -q` 要全過，行為改變要補測試。
- **範圍**：只做 issue 寫的事。發現其他問題就在 issue 留言提出，不要順手改。

## 做不到的事要交回給人

標了「需人工」的 issue（接線、機構、採購、拍照、主機與帳號設定）agent 不能獨立完成。程式部分寫完後，在 PR 或 issue 寫清楚人要在實機上做哪些步驟、看到什麼才算過。

## 完成

開 PR，標題照 commit 格式，描述寫 `Closes #N` 並填完 PR 範本；review 由 @whyhugo 負責，不要自己合併。
