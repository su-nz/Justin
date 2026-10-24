"""模擬器：沒有硬體也能 Demo 整套系統。

- 在師大和平校區 / 古亭 / 公館一帶註冊 16 個桶（座標為示意）
- 回填過去 N 小時的遙測（依時段的投入率：晚間尖峰，呼應 Paper 3 的時間樣態）
- 注入幾個事件：大袋被拒、地面垃圾（由後端融合為 overflow / illegal_dumping）、感測器遮擋
- --live 之後持續每 30 秒推進一次

用法：
  python sim/simulate.py --api http://localhost:8000 --admin dev-admin-token --hours 10
  python sim/simulate.py --api http://localhost:8000 --live
"""
import argparse
import json
import math
import random
import time
from pathlib import Path

import httpx

BINS = [
    ("NTNU-01", "師大本部 正門", 25.0262, 121.5275, 60, True),
    ("NTNU-02", "師大路 夜市口", 25.0247, 121.5288, 60, True),
    ("NTNU-03", "龍泉街口", 25.0237, 121.5291, 60, False),
    ("NTNU-04", "和平東路 公車站", 25.0260, 121.5300, 60, False),
    ("NTNU-05", "古亭站 2號出口", 25.0265, 121.5229, 60, True),
    ("NTNU-06", "古亭站 5號出口", 25.0257, 121.5222, 60, False),
    ("NTNU-07", "金華街 公園", 25.0306, 121.5276, 60, False),
    ("NTNU-08", "泰順街口", 25.0215, 121.5295, 60, False),
    ("NTNU-09", "台電大樓站", 25.0207, 121.5282, 60, True),
    ("NTNU-10", "公館站 1號出口", 25.0146, 121.5341, 60, True),
    ("NTNU-11", "公館 水源市場", 25.0132, 121.5329, 60, False),
    ("NTNU-12", "師大分部 校門", 25.0072, 121.5374, 60, False),
    ("NTNU-13", "溫州街口", 25.0196, 121.5326, 60, False),
    ("NTNU-14", "大安森林公園 西側", 25.0300, 121.5340, 60, False),
    ("NTNU-15", "新生南路 加油站旁", 25.0220, 121.5335, 60, False),
    ("NTNU-16", "永康街口", 25.0328, 121.5298, 60, False),
]
STATE_FILE = Path(__file__).with_name(".tokens.json")


def hourly_rate(h: float) -> float:
    """每小時填充百分比的基準：早通勤小峰、午餐、晚間 18–23 時大峰、凌晨低。"""
    return 1.5 + 3 * math.exp(-((h - 8) / 1.2) ** 2) + 4 * math.exp(-((h - 12.5) / 1.5) ** 2) \
        + 7 * math.exp(-((h - 20.5) / 2.2) ** 2)


def register(api, admin):
    tokens = {}
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {admin}"}) as c:
        for bid, name, lat, lng, cap, gate in BINS:
            r = c.post("/api/bins", json=dict(id=bid, name=name, lat=lat, lng=lng, capacity_l=cap,
                                               depth_mm=700, has_gate=gate))
            r.raise_for_status()
            tokens[bid] = r.json()["device_token"]
    STATE_FILE.write_text(json.dumps(tokens))
    return tokens


def run(api, tokens, hours, live, seed):
    rnd = random.Random(seed)
    busy = {b[0]: rnd.uniform(0.5, 1.8) for b in BINS}       # 每桶人流係數
    busy["NTNU-02"], busy["NTNU-05"], busy["NTNU-10"] = 2.6, 2.3, 2.4  # 夜市、捷運站
    fill = {b[0]: rnd.uniform(5, 40) for b in BINS}
    now = time.time()
    step = 600  # 10 分鐘一筆
    t = now - hours * 3600
    with httpx.Client(base_url=api) as c:
        def send(bid, ts, blocked=False):
            depth = 700
            dist = int(depth - fill[bid] / 100 * (depth - 50))
            c.post("/api/telemetry", headers={"X-Device-Token": tokens[bid]},
                   json=dict(bin_id=bid, ts=ts, fill_pct=round(fill[bid], 1), dist_mm=dist,
                             weight_g=round(fill[bid] * 85 + rnd.gauss(0, 50)),
                             battery_mv=rnd.randint(3700, 4100), rssi=rnd.randint(-85, -55),
                             blocked=blocked)).raise_for_status()

        def advance(ts, dt):
            h = time.localtime(ts).tm_hour + time.localtime(ts).tm_min / 60
            for bid in fill:
                fill[bid] = min(100.0, fill[bid] + hourly_rate(h) * busy[bid] * dt / 3600 * rnd.uniform(0.6, 1.4))

        while t < now:
            advance(t, step)
            for bid in fill:
                send(bid, t, blocked=(bid == "NTNU-15" and now - t < 3 * 3600))
            t += step
        print("backfill done:", {k: round(v) for k, v in sorted(fill.items())})

        ev = lambda bid, typ, conf, detail: c.post(  # noqa: E731
            "/api/events", headers={"X-Device-Token": tokens[bid]},
            json=dict(bin_id=bid, type=typ, confidence=conf, detail=detail)).json()
        print(ev("NTNU-05", "bag_rejected", 0.91, {"cls": "garbage_bag", "area_ratio": 0.62}))
        print(ev("NTNU-02", "ground_waste", 0.88, {"coverage": 0.21}))   # 夜市口應已滿 → overflow
        print(ev("NTNU-09", "ground_waste", 0.84, {"coverage": 0.12}))   # 未滿 → illegal_dumping

        while live:
            time.sleep(30)
            advance(time.time(), 30 * 20)  # 加速 20 倍
            for bid in fill:
                send(bid, time.time())
            print(time.strftime("%H:%M:%S"), {k: round(v) for k, v in sorted(fill.items())})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--admin", default="dev-admin-token")
    ap.add_argument("--hours", type=float, default=10)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    toks = register(a.api, a.admin)
    run(a.api, toks, a.hours, a.live, a.seed)
