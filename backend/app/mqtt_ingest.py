"""MQTT 訂閱：Wi-Fi 感測節點 / 視覺閘門直接發到 broker。

裝置身分由 broker 驗證（每桶一組帳密 + ACL，只能寫 smartbin/<自己的 id>/#），
因此這裡以 topic 中的 bin_id 為準，忽略 payload 內的 bin_id，避免冒名。
"""
from __future__ import annotations

import json
import logging
import os
import threading

import paho.mqtt.client as mqtt

log = logging.getLogger("mqtt")


def start(on_telemetry, on_event) -> None:
    from .main import EventIn, TelemetryIn  # 延遲匯入避免循環

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smartbin-backend")
    if os.environ.get("MQTT_USER"):
        cli.username_pw_set(os.environ["MQTT_USER"], os.environ.get("MQTT_PASS", ""))
    if os.environ.get("MQTT_TLS") == "1":
        cli.tls_set(ca_certs=os.environ.get("MQTT_CA"))

    def on_connect(c, *_):
        c.subscribe([("smartbin/+/telemetry", 1), ("smartbin/+/event", 1)])
        log.info("MQTT subscribed")

    def on_message(_c, _u, m):
        try:
            _, bin_id, kind = m.topic.split("/")
            data = json.loads(m.payload)
            data["bin_id"] = bin_id
            if kind == "telemetry":
                on_telemetry(TelemetryIn(**data))
            else:
                on_event(EventIn(**data))
        except Exception as e:  # 單筆壞資料不能讓訂閱掛掉
            log.warning("drop %s: %s", m.topic, e)

    cli.on_connect, cli.on_message = on_connect, on_message
    cli.connect_async(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", "1883")))
    threading.Thread(target=cli.loop_forever, daemon=True).start()
