"""核心邏輯單元測試：python -m pytest tests -q（在 smartbin/ 下執行）"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "edge" / "vision-gate")]

from app import logic, routing  # noqa: E402
import gate  # noqa: E402


def test_hysteresis_no_flapping():
    s = None
    seq = []
    for f in [70, 76, 73, 74, 71, 69, 91, 87, 86, 84]:
        s = logic.classify(f, s)
        seq.append(s)
    assert seq == ["available", "near_full", "near_full", "near_full", "near_full",
                   "available", "full", "full", "full", "near_full"]


def test_fill_rate_ignores_collection_drop():
    h = 3600
    samples = [(0, 50), (h, 70), (2 * h, 90), (2.5 * h, 5), (3 * h, 10), (3.5 * h, 15), (4 * h, 20)]
    assert abs(logic.fill_rate(samples) - 10.0) < 0.5   # 清運後 10%/h，不是被落差拉成負的


def test_fusion():
    assert logic.fuse_ground_event(95) == "overflow"
    assert logic.fuse_ground_event(40) == "illegal_dumping"


def test_routing_mandatory_and_saving():
    bins = [dict(id=f"B{i}", name=f"B{i}", lat=25.02 + i * 0.002, lng=121.53 + (i % 3) * 0.002,
                 capacity_l=60, state="available", fill_pct=20, rate_pct_per_h=1, open_events=[])
            for i in range(10)]
    bins[2].update(state="full", fill_pct=95)
    bins[7].update(open_events=["illegal_dumping"])
    p = routing.plan_routes((25.02, 121.54), bins, vehicles=1, horizon_h=2)
    visited = {s["bin_id"] for r in p.routes for s in r["path"] if "bin_id" in s}
    assert visited == {"B2", "B7"}
    assert p.total_m < p.baseline_m and p.saving_pct > 0


def test_coverage_union():
    roi = (0, 0, 1, 1)
    # 兩個完全重疊的框不能算兩倍
    assert abs(gate.coverage([(0, 0, .5, .5), (0, 0, .5, .5)], roi) - 0.25) < 0.02


def test_gate_rejects_bag_and_opens_for_cup():
    cfg = gate.Cfg()
    act = gate.Actuators(cfg, dry=True)
    up = gate.Uploader(cfg, offline=True)
    g = gate.Gate(cfg, act, up)
    cup = [("cup", .9, (.45, .1, .52, .2))]
    g.step(cup, 0); g.step(cup, 1)
    assert act.is_open
    g.step([], 10)
    assert not act.is_open
    bag = [("garbage_bag", .9, (.37, .06, .63, .34))]
    g.step(bag, 20); g.step(bag, 21)
    assert not act.is_open
    ev = up.q.get_nowait()
    assert ev["type"] == "bag_rejected"
