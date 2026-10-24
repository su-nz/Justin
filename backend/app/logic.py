"""核心判斷邏輯：桶子狀態、滿溢預測、攝影機×液位感測融合。

設計重點（對應簡報 Challenges 與 Paper 3 的改進方向）：
- 狀態有遲滯（hysteresis），避免在 80% 附近來回跳動造成假警報。
- 滿溢預測只用「最近一次清運之後」的資料做線性回歸，避免把清運的落差算進去。
- 地面有垃圾時，用液位判斷是「桶滿外溢 overflow」還是「桶未滿卻亂丟 illegal_dumping」。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

# ---- 門檻（可用環境變數覆寫，見 config.py） ----
NEAR_FULL_PCT = 75.0      # 進入「將滿」
FULL_PCT = 90.0           # 進入「已滿」：市民地圖不再導向此桶
HYSTERESIS_PCT = 5.0      # 離開狀態需降回門檻 - 5%
OFFLINE_AFTER_S = 45 * 60 # 超過 45 分鐘沒心跳 → offline（韌體心跳 30 分鐘）
COLLECTION_DROP_PCT = 30.0  # 兩筆之間掉超過 30% 視為已清運


@dataclass
class BinStatus:
    state: str               # available / near_full / full / offline / fault
    fill_pct: float | None
    rate_pct_per_h: float | None
    hours_to_full: float | None
    last_seen: float | None


def classify(fill: float, prev_state: str | None) -> str:
    """含遲滯的狀態判斷。"""
    if prev_state == "full" and fill >= FULL_PCT - HYSTERESIS_PCT:
        return "full"
    if fill >= FULL_PCT:
        return "full"
    if prev_state in ("near_full", "full") and fill >= NEAR_FULL_PCT - HYSTERESIS_PCT:
        return "near_full"
    if fill >= NEAR_FULL_PCT:
        return "near_full"
    return "available"


def since_last_collection(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """samples: [(ts, fill)] 依時間排序。回傳最後一次清運之後的樣本。"""
    start = 0
    for i in range(1, len(samples)):
        if samples[i - 1][1] - samples[i][1] >= COLLECTION_DROP_PCT:
            start = i
    return samples[start:]


def fill_rate(samples: list[tuple[float, float]]) -> float | None:
    """最小平方法估計填充速率（%/小時）。樣本不足或時間跨度 < 20 分鐘回傳 None。"""
    s = since_last_collection(samples)
    if len(s) < 3 or s[-1][0] - s[0][0] < 1200:
        return None
    t0 = s[0][0]
    xs = [(t - t0) / 3600 for t, _ in s]
    ys = [f for _, f in s]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return max(slope, 0.0)


def status_of(samples: list[tuple[float, float]], blocked: bool, prev_state: str | None = None,
              now: float | None = None) -> BinStatus:
    now = now or time.time()
    if not samples:
        return BinStatus("offline", None, None, None, None)
    last_ts, last_fill = samples[-1]
    rate = fill_rate(samples)
    htf = None
    if rate and rate > 0.01:
        htf = max((FULL_PCT - last_fill) / rate, 0.0)
    if now - last_ts > OFFLINE_AFTER_S:
        state = "offline"
    elif blocked:
        state = "fault"
    else:
        state = classify(last_fill, prev_state)
    return BinStatus(state, round(last_fill, 1), None if rate is None else round(rate, 2),
                     None if htf is None else round(htf, 2), last_ts)


def fuse_ground_event(fill_pct: float | None) -> str:
    """Paper 3 改進：攝影機看到地面垃圾時，融合液位感測判斷成因。"""
    if fill_pct is None:
        return "ground_waste_unknown"
    return "overflow" if fill_pct >= FULL_PCT else "illegal_dumping"


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
