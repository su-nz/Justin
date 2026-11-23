#!/bin/bash
# 閘門啟動腳本（#41）：依模式檔選擇「劇本」或「相機即時辨識」，gate.py 結束就重新啟動。
# 調度台送 gate_mode 指令時，gate.py 會改寫模式檔後結束，這裡就用新模式重開。
#
# 環境變數（都有預設值）：
#   GATE_MODE_FILE  模式檔，內容 mock 或 camera（預設 /tmp/smartbin-gate-mode，重開機回到劇本）
#   GATE_SCENARIO   劇本檔（預設 scenarios/demo.json）
#   GATE_MODEL      相機模式的 ONNX 模型（預設 ~/yolo11n.onnx）
# 其他參數原樣傳給 gate.py，例如：run-gate.sh --open-angle 90 --close-angle 0
DIR="$(cd "$(dirname "$0")" && pwd)"
MODE_FILE="${GATE_MODE_FILE:-/tmp/smartbin-gate-mode}"
export GATE_MODE_FILE="$MODE_FILE"
while true; do
  mode="$(cat "$MODE_FILE" 2>/dev/null || echo mock)"
  start=$(date +%s)
  if [ "$mode" = camera ]; then
    python3 "$DIR/gate.py" --backend onnx --model "${GATE_MODEL:-$HOME/yolo11n.onnx}" --labels coco --fps 10 "$@"
    # 相機或模型有問題時會馬上結束：退回劇本，不要一直重試
    if [ $(( $(date +%s) - start )) -lt 15 ] && [ "$(cat "$MODE_FILE" 2>/dev/null)" = camera ]; then
      echo "相機模式啟動失敗，退回劇本模式" >&2
      echo mock > "$MODE_FILE"
    fi
  else
    python3 "$DIR/gate.py" --backend mock --scenario "${GATE_SCENARIO:-$DIR/scenarios/demo.json}" --drive --fps 1 "$@"
  fi
  sleep 2
done
