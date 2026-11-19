# 協作規範

給組員和 agent 共用。總覽與分工見 issue #1。

## 流程

1. **從 issue 開始**：每項工作都對應一張 issue。沒有 issue 就先開一張（用 Task 範本）。
2. **開分支**：從最新的 `main` 開，名稱 `<type>/<issue 號>-<短描述>`，例如 `feat/5-node-pi`、`fix/13-servo-jitter`。
3. **commit**：照下面的格式，小步提交。
4. **開 PR**：照 PR 範本填，描述寫 `Closes #N`。CODEOWNERS 會自動請 @whyhugo 和 @su-nz review。
5. **review 與合併**：Hugo 審過後用 **Squash and merge** 合併，issue 自動關閉。不要自己合併自己的 PR。
6. **不改程式的工作**（接線、採購、拍照）：在 issue 留言附照片或影片並標記 @whyhugo，由 Hugo 驗收後關閉。

## Commit 格式

```
<type>(<scope>): <摘要>

<為什麼這樣改，可省略>

Refs #N
```

- 摘要用中文或英文都可以，50 字以內，不加句號。
- `Refs #N` 只做關聯；關閉 issue 由 PR 的 `Closes #N` 負責。
- Squash 合併時用 PR 標題當 commit 訊息，所以 **PR 標題也照這個格式**。

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文件 |
| `test` | 只加或改測試 |
| `refactor` | 不改行為的重構 |
| `chore` | 設定、相依套件、雜項 |

| scope | 範圍 |
|---|---|
| `sensor` | `edge/pi-demo/`、`firmware/sensor-node/` |
| `gate` | `edge/vision-gate/`（擋板、相機、gate.py） |
| `vision` | 模型訓練與匯出 |
| `backend` | `backend/app/` |
| `line` | `backend/app/line_bot.py` |
| `web` | `backend/static/` |
| `deploy` | `deploy/`、主機設定 |
| `repo` | `.github/`、規範文件 |

範例：

```
feat(sensor): 新增 node_pi.py 每 30 秒上傳滿度

Refs #5
```

## PR 規則

- 一個 PR 對應一張 issue，改動盡量小。
- PR 會自動跑兩項檢查，都要通過才能合併：
  - **PR 標題格式**：照上面的 commit 格式，type 和 scope 只能用表格裡的值。沒過就改標題，不用重開 PR。
  - **測試**：`python -m pytest tests -q`。改到行為就補測試；本機先跑過再推。
- 改到硬體行為的（GPIO、擋板、相機），PR 附照片或影片。
- 改到 API、環境變數、GPIO 分配，同步更新 README 和 issue #1 的介面約定。

## 不能進 repo 的東西

- token、密碼、金鑰：`DEVICE_TOKEN`、`ADMIN_TOKEN`、LINE channel secret / access token、`.pem`。一律用環境變數，私訊交給需要的人。
- 照片資料集、模型權重（`.pt`、`.onnx`、`.rpk`）：放共用雲端，PR 只放連結和訓練設定。
- 不小心 commit 了 token：馬上通知 Hugo 換掉那組 token，只刪檔案不夠。

## Issue 怎麼寫

用 Task 範本：背景、要做什麼、驗收條件、前置 issue。標籤用領域（感測、閘門、伺服器、網頁、視覺、統籌）；需要實體操作或帳號、agent 做不了的，加「需人工」。Milestone 選對應的關卡。
