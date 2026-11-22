# Demo 後端部署（HTTP、無 MQTT）

以下步驟在 Demo 主機的 repo checkout 執行。服務只綁定主機的 `127.0.0.1:8000`；由主機上的 Caddy、Nginx 或既有 HTTPS proxy 將公開網址轉發到該位址。若 proxy 在另一個容器，請讓兩個服務加入同一個 Docker network，並只對 proxy 開放後端連線。

## 1. 設定秘密與公開網址

在 repo 根目錄執行；產生至少 32 bytes 的隨機 admin token，並保護本機環境檔：

```bash
cd deploy
umask 077
ADMIN_TOKEN="$(openssl rand -hex 32)"
read -r -p "Demo HTTPS URL (for example https://demo.example.edu): " PUBLIC_URL
cat > .env <<EOF
ADMIN_TOKEN=${ADMIN_TOKEN}
PUBLIC_URL=${PUBLIC_URL}
EOF
set -a
. ./.env
set +a
```

輸入實際 HTTPS 網址。`.env` 僅留在主機，不要加入 repo、貼到 issue/PR 或傳到群組。若 Demo 使用 LINE webhook，將 `LINE_CHANNEL_SECRET` 和 `LINE_CHANNEL_ACCESS_TOKEN` 也加到此檔。

## 2. 啟動後端

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

這份設定只啟動 `backend`，不設定 `MQTT_HOST`，所以不會連 MQTT。SQLite 位於 Compose named volume `smartbin-demo-data`；容器重建後資料仍保留。部署後確認 proxy 已提供有效 HTTPS 憑證，並將 `PUBLIC_URL` 設為同一個公開網址。

## 3. 驗證健康狀態並建立 NTNU-01

`/api/health` 和 `/api/bins` 不需要 admin token：

```bash
curl --fail-with-body --silent --show-error "$PUBLIC_URL/api/health"
curl --fail-with-body --silent --show-error "$PUBLIC_URL/api/bins"
```

設定 Demo 地點的實際座標，以及 Kaiwei 確認的實體桶深（毫米），再呼叫管理 API。請勿猜測座標或深度：

```bash
# 先填入實際 Demo 座標與 Kaiwei 確認的桶深（毫米），再執行 curl。
BIN_LAT=
BIN_LNG=
DEPTH_MM=
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"NTNU-01\",\"name\":\"NTNU-01\",\"lat\":$BIN_LAT,\"lng\":$BIN_LNG,\"depth_mm\":$DEPTH_MM,\"has_gate\":true}" \
  "$PUBLIC_URL/api/bins"
```

建立成功的回應會包含 `device_token`，只顯示這一次；資料庫只存 token 的 SHA-256。請透過既有的私密管道把 device token 交給 NZ 和 Kaiwei，把 `ADMIN_TOKEN` 交給 Hugo 和綜觀全局；不要把任何 token 放進 repo 或 PR。

## 4. 驗證桶資料與 HTTP telemetry

將建立時回傳的 device token 暫存於安全位置，再測試：

```bash
curl --fail-with-body --silent --show-error "$PUBLIC_URL/api/bins"
DEVICE_TOKEN=
curl --fail-with-body --silent --show-error -X POST \
  -H "X-Device-Token: $DEVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bin_id":"NTNU-01","fill_pct":50}' \
  "$PUBLIC_URL/api/telemetry"
```

`POST /api/telemetry` 成功時回應 HTTP 204。重新載入 `$PUBLIC_URL/`，確認地圖顯示 NTNU-01。健康檢查、桶清單和地圖均應使用 HTTPS 公開網址。

## 維護

- 查看服務：`docker compose -f docker-compose.demo.yml ps`；查看日誌：`docker compose -f docker-compose.demo.yml logs --tail=100 backend`。
- 更新程式：`docker compose -f docker-compose.demo.yml up -d --build`。
- 定期備份 volume `smartbin-demo-data`；不要用 `docker compose down -v`，該選項會刪除 SQLite 資料。
- `ADMIN_TOKEN` 和 device token 都是秘密。若懷疑外洩，請安排更新 token 並重新建立對應資料。
