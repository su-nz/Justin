"""LINE 官方帳號：市民免安裝 App，傳「位置」就回最近有空位的垃圾桶。

設定：LINE Developers 建 Messaging API channel，把 Webhook URL 設為 https://<你的網域>/line/webhook，
環境變數填 LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

import httpx

SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
PWA_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000") + "/static/index.html"


def verify(body: bytes, signature: str) -> bool:
    if not SECRET:
        return False
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def _ask_location() -> dict:
    return {"type": "text", "text": "請分享你的位置，我幫你找最近還有空位的垃圾桶 🗑️",
            "quickReply": {"items": [{"type": "action", "action": {"type": "location", "label": "傳送我的位置"}}]}}


def _answer(bins: list[dict]) -> list[dict]:
    if not bins:
        return [{"type": "text", "text": f"附近暫時沒有可用的垃圾桶😢 可到地圖查看：{PWA_URL}"}]
    b = bins[0]
    label = {"available": "有空位", "near_full": "快滿了"}.get(b["state"], b["state"])
    msgs = [{"type": "location", "title": f"{b['name']}（{label}）",
             "address": f"約 {b['distance_m']} 公尺，目前 {b['fill_pct']:.0f}%",
             "latitude": b["lat"], "longitude": b["lng"]}]
    if len(bins) > 1:
        others = "\n".join(f"・{x['name']} {x['distance_m']}m（{x['fill_pct']:.0f}%）" for x in bins[1:])
        msgs.append({"type": "text", "text": f"其他選擇：\n{others}\n\n完整地圖：{PWA_URL}"})
    return msgs


async def _reply(reply_token: str, messages: list[dict]) -> None:
    if not TOKEN:
        return
    async with httpx.AsyncClient(timeout=5) as cli:
        await cli.post("https://api.line.me/v2/bot/message/reply",
                       headers={"Authorization": f"Bearer {TOKEN}"},
                       json={"replyToken": reply_token, "messages": messages[:5]})


async def handle(payload: dict, nearest_fn) -> None:
    for ev in payload.get("events", []):
        if ev.get("type") != "message" or "replyToken" not in ev:
            continue
        msg = ev["message"]
        if msg.get("type") == "location":
            await _reply(ev["replyToken"], _answer(nearest_fn(msg["latitude"], msg["longitude"], 3)))
        else:
            await _reply(ev["replyToken"], [_ask_location()])
