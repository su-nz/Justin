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


def test_mock_drives_hardware_only_with_drive_flag():
    ns = lambda **kw: type("A", (), {"dry": False, "drive": False, "backend": "mock", **kw})()
    assert gate.actuators_dry(ns()) is True                    # 劇本預設不碰 GPIO
    assert gate.actuators_dry(ns(drive=True)) is False         # --drive 驅動實體元件
    assert gate.actuators_dry(ns(drive=True, dry=True)) is True  # --dry 優先
    assert gate.actuators_dry(ns(backend="onnx")) is False


def _fake_gpiozero(monkeypatch, pigpio_ok):
    import types
    made = {}

    class Dev:
        def __init__(self, pin, **kw):
            self.pin, self.kw, self.angle = pin, kw, None
            made.setdefault(type(self).__name__, []).append(self)

        def on(self): pass
        def off(self): pass
        def blink(self, **kw): pass
        def beep(self, **kw): pass
        def detach(self): pass

    class AngularServo(Dev):
        history = []

        @property
        def angle(self):
            return self.history[-1] if self.history else None

        @angle.setter
        def angle(self, v):
            if v is not None:
                self.history.append(v)
    class LED(Dev): pass
    class Buzzer(Dev): pass

    class PiGPIOFactory:
        def __init__(self):
            if not pigpio_ok:
                raise OSError("failed to connect to localhost:8888")

    gz = types.ModuleType("gpiozero")
    gz.AngularServo, gz.LED, gz.Buzzer = AngularServo, LED, Buzzer
    pins = types.ModuleType("gpiozero.pins")
    pig = types.ModuleType("gpiozero.pins.pigpio")
    pig.PiGPIOFactory = PiGPIOFactory
    monkeypatch.setitem(sys.modules, "gpiozero", gz)
    monkeypatch.setitem(sys.modules, "gpiozero.pins", pins)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.pigpio", pig)
    return made, PiGPIOFactory


def test_actuators_servo_uses_pigpio_and_configured_angles(monkeypatch):
    made, factory = _fake_gpiozero(monkeypatch, pigpio_ok=True)
    cfg = gate.Cfg()
    cfg.servo_open_angle, cfg.servo_close_angle = 100, 10
    act = gate.Actuators(cfg, dry=False)
    servo = made["AngularServo"][0]
    assert servo.pin == 18
    assert servo.kw["min_pulse_width"] == 0.0005 and servo.kw["max_pulse_width"] == 0.0025
    assert (servo.kw["min_angle"], servo.kw["max_angle"]) == (0, 180)
    assert isinstance(servo.kw["pin_factory"], factory)
    assert servo.angle == 10       # 建立時先關閘
    act.open()
    assert servo.angle == 100
    act.close()
    assert servo.angle == 10


def test_actuators_fall_back_without_pigpiod(monkeypatch):
    made, _ = _fake_gpiozero(monkeypatch, pigpio_ok=False)
    gate.Actuators(gate.Cfg(), dry=False)
    assert made["AngularServo"][0].kw["pin_factory"] is None  # pigpiod 沒開也能跑，只是會抖


def test_mock_drive_runs_demo_scenario(monkeypatch):
    made, _ = _fake_gpiozero(monkeypatch, pigpio_ok=True)
    monkeypatch.setattr(gate.time, "sleep", lambda s: None)
    scenario = str(ROOT / "edge" / "vision-gate" / "scenarios" / "demo.json")
    gate.main(["--backend", "mock", "--scenario", scenario, "--offline", "--sim-dt", "30",
               "--drive", "--open-angle", "80"])
    servo = made["AngularServo"][0]
    assert made["LED"] and made["Buzzer"]
    assert 80 in servo.history          # 劇本裡的杯子讓實體擋板打開到 --open-angle
    assert servo.history[-1] == 0       # 結束時關閘
