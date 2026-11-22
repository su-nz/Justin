"""核心邏輯單元測試：python -m pytest tests -q（在 smartbin/ 下執行）"""
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _gate():
    cfg = gate.Cfg()
    act = gate.Actuators(cfg, dry=True)
    up = gate.Uploader(cfg, offline=True)
    return act, up, gate.Gate(cfg, act, up)


def test_offline_threshold_matches_deck():
    now = 100_000
    assert logic.status_of([(now - 64 * 60, 10)], False, now=now).state == "available"
    assert logic.status_of([(now - 66 * 60, 10)], False, now=now).state == "offline"


def test_large_hand_holding_cup_still_opens():
    act, up, g = _gate()
    frame = [("cup", .9, (.45, .12, .52, .24)), ("hand", .9, (.38, .06, .60, .28))]
    g.step(frame, 0); g.step(frame, 1)
    assert act.is_open and up.q.empty()


def test_bag_larger_than_slot_is_rejected_even_with_cup():
    act, up, g = _gate()
    frame = [("garbage_bag", .93, (.25, 0, .75, .5)), ("cup", .9, (.45, .1, .52, .2))]
    g.step(frame, 0); g.step(frame, 1)
    assert not act.is_open
    assert up.q.get_nowait()["type"] == "bag_rejected"


def test_full_bin_keeps_gate_shut_for_cup():
    act, up, g = _gate()
    g.bin_full = True
    cup = [("cup", .9, (.45, .1, .52, .2))]
    g.step(cup, 0); g.step(cup, 1)
    assert not act.is_open
    g.bin_full = False
    g.step(cup, 2)
    assert act.is_open


def test_every_bag_attempt_gets_feedback_but_one_upload():
    act, up, g = _gate()
    blinks = []
    act.reject = lambda: blinks.append(1)
    bag = [("garbage_bag", .9, (.37, .06, .63, .34))]
    g.step(bag, 0); g.step(bag, 1); g.step([], 5); g.step(bag, 30); g.step(bag, 31)
    assert len(blinks) == 2 and up.q.qsize() == 1


def test_coco_labels_map_to_gate_classes():
    assert len(gate.COCO_NAMES) == 80 and gate.COCO_NAMES[41] == "cup" and gate.COCO_NAMES[39] == "bottle"
    assert [gate.coco_to_gate(n) for n in ("cup", "wine glass", "person", "handbag", "dog")] == \
        ["cup", "cup", "hand", "garbage_bag", "other"]


def test_unknown_coco_object_never_opens_but_big_one_is_rejected():
    act, up, g = _gate()
    small = [("other", .9, (.45, .1, .55, .2))]
    g.step(small, 0); g.step(small, 1)
    assert not act.is_open and up.q.qsize() == 0
    big = [("other", .9, (.36, .06, .64, .34))]
    g.step(big, 2); g.step(big, 3)
    assert not act.is_open and up.q.qsize() == 1


def test_camera_source_index_or_path():
    assert gate.camera_source("0") == 0
    assert gate.camera_source(" 2 ") == 2
    assert gate.camera_source("/dev/video2") == "/dev/video2"


def test_effective_fps_counts_only_processed_frames(monkeypatch, caplog):
    elapsed = 0.0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(gate, "time", SimpleNamespace(
        time=lambda: elapsed, perf_counter=lambda: elapsed, sleep=advance,
    ))
    monkeypatch.setattr(gate.Uploader, "start", lambda self: None)
    detector = gate.OnnxDetector.__new__(gate.OnnxDetector)
    detector.cap = SimpleNamespace(read=lambda: (False, None))
    failed_frame = detector.detect

    def processed_frame():
        advance(0.2)
        return []

    # One window with no frames, one with a mix, and one with valid empty detections.
    frames = iter([failed_frame] * 30 + [processed_frame] * 30)

    def detect():
        frame = next(frames, None)
        return frame() if frame is not None else None

    monkeypatch.setattr(gate, "OnnxDetector", lambda *args, **kwargs: SimpleNamespace(detect=detect))
    monkeypatch.setattr(sys, "argv", ["gate.py", "--backend", "onnx", "--offline", "--dry"])
    with caplog.at_level("INFO", logger="gate"):
        gate.main()

    assert "實際 0.0 FPS（有效影格 0/20" in caplog.text
    assert "實際 1.7 FPS（有效影格 10/20" in caplog.text
    assert "實際 2.5 FPS（有效影格 20/20" in caplog.text
