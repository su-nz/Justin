"""核心邏輯單元測試：python -m pytest tests -q（在 smartbin/ 下執行）"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "edge" / "vision-gate")]

from app import db, logic, main, routing  # noqa: E402
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


def test_large_light_bin_still_uses_fill_level(monkeypatch):
    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    status = logic.status_of([(100_000, 95)], False, now=100_001, weight_g=200)
    assert status.state == "full"
    assert status.weight_g == 200


def test_small_heavy_bin_is_full_when_weight_threshold_is_enabled(monkeypatch):
    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    status = logic.status_of([(100_000, 20)], False, now=100_001, weight_g=1200)
    assert status.state == "full"


def test_weight_threshold_is_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("FULL_WEIGHT_G", raising=False)
    status = logic.status_of([(100_000, 20)], False, now=100_001, weight_g=1200)
    assert status.state == "available"


def test_blocked_sensor_status_keeps_latest_weight(monkeypatch):
    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    status = logic.status_of([(100_000, 20)], True, now=100_001, weight_g=1200)
    assert status.state == "fault"
    assert status.weight_g == 1200
    assert status.full_for_gate


def test_blocked_sensor_needs_weight_and_threshold_to_lock_gate(monkeypatch):
    samples = [(100_000, 100)]
    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    for weight in (None, 800):
        status = logic.status_of(samples, True, now=100_001, weight_g=weight)
        assert status.state == "fault"
        assert not status.full_for_gate

    monkeypatch.delenv("FULL_WEIGHT_G")
    status = logic.status_of(samples, True, now=100_001, weight_g=1200)
    assert status.state == "fault"
    assert not status.full_for_gate


def test_gate_watcher_keeps_weight_full_bin_locked_when_sensor_is_blocked(monkeypatch, tmp_path):
    import types
    import httpx

    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "smartbin.db"))
    db.init_db()
    now = time.time()
    with db.conn() as c:
        c.execute("INSERT INTO bins(id,name,lat,lng,token_hash,created_at) VALUES(?,?,?,?,?,?)",
                  ("NTNU-01", "師大一號", 25.0, 121.5, "", now))
        c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,weight_g,blocked) VALUES(?,?,?,?,?)",
                  ("NTNU-01", now, 20, 1200, 1))
    bin_data = main.list_bins()
    assert bin_data[0]["state"] == "fault"
    assert bin_data[0]["full_for_gate"] is True

    monkeypatch.setattr(httpx, "get", lambda *a, **k: types.SimpleNamespace(json=lambda: bin_data))
    act, up, g = _gate()
    g.cfg.bin_id = "NTNU-01"
    gate.BinStateWatcher(g.cfg, g).poll()
    assert g.bin_full


def test_all_bins_include_latest_weight_and_null_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "smartbin.db"))
    db.init_db()
    now = time.time()
    with db.conn() as c:
        c.execute("INSERT INTO bins(id,name,lat,lng,token_hash,created_at) VALUES(?,?,?,?,?,?)",
                  ("NTNU-01", "師大一號", 25.0, 121.5, "", now))
        c.execute("INSERT INTO bins(id,name,lat,lng,token_hash,created_at) VALUES(?,?,?,?,?,?)",
                  ("EMPTY", "無資料", 25.0, 121.5, "", now))
        c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,weight_g,blocked) VALUES(?,?,?,?,?)",
                  ("NTNU-01", now - 2 * 24 * 3600, 20, 750.5, 0))
    bins = {b["id"]: b for b in main._all_bins()}
    assert bins["NTNU-01"]["weight_g"] == 750.5
    assert bins["EMPTY"]["weight_g"] is None


def test_missing_latest_weight_does_not_reuse_heavy_reading(monkeypatch, tmp_path):
    monkeypatch.setenv("FULL_WEIGHT_G", "1000")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "smartbin.db"))
    db.init_db()
    now = time.time()
    with db.conn() as c:
        c.execute("INSERT INTO bins(id,name,lat,lng,token_hash,created_at) VALUES(?,?,?,?,?,?)",
                  ("NTNU-01", "師大一號", 25.0, 121.5, "", now))
        c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,weight_g,blocked) VALUES(?,?,?,?,?)",
                  ("NTNU-01", now - 2, 20, 1200, 0))
        c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,weight_g,blocked) VALUES(?,?,?,?,?)",
                  ("NTNU-01", now - 1, 3, None, 0))

    bins = {b["id"]: b for b in main._all_bins()}
    assert bins["NTNU-01"]["state"] == "available"
    assert bins["NTNU-01"]["weight_g"] is None


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
        events = []

        def __init__(self, pin, **kw):
            super().__init__(pin, **kw)
            # gpiozero 建立時就送出 initial_angle，沒指定時預設是 0°
            initial = kw.get("initial_angle", 0)
            if initial is not None:
                self.history.append(initial)

        def detach(self):
            self.events.append(("detach", self.angle))

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
    assert servo.history[0] == 10  # 第一個 PWM 就是校正後的關門角度，不會先跑到 0°
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


def test_admin_bins_needs_token_and_keeps_public_api_small(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import db, main
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    adm = {"Authorization": f"Bearer {main.ADMIN_TOKEN}"}
    with TestClient(main.app) as cl:
        tok = cl.post("/api/bins", headers=adm, json={"id": "NTNU-01", "name": "x", "lat": 25.0, "lng": 121.5}).json()["device_token"]
        cl.post("/api/telemetry", headers={"X-Device-Token": tok}, json={"bin_id": "NTNU-01", "fill_pct": 40})
        assert cl.get("/api/admin/bins").status_code == 401
        full = cl.get("/api/admin/bins", headers=adm).json()[0]
        assert full["last_seen"] and full["open_events"] == []
        pub = cl.get("/api/bins").json()[0]
        assert "last_seen" not in pub and "open_events" not in pub


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


def test_shutdown_holds_close_signal_before_detach(monkeypatch):
    made, _ = _fake_gpiozero(monkeypatch, pigpio_ok=True)
    cfg = gate.Cfg()
    act = gate.Actuators(cfg, dry=False)
    servo = made["AngularServo"][0]
    monkeypatch.setattr(gate.time, "sleep", lambda s: servo.events.append(("sleep", s)))
    act.open()
    act.shutdown()   # 擋板開著時結束
    assert servo.events == [("sleep", cfg.servo_settle_s), ("detach", cfg.servo_close_angle)]
    assert cfg.servo_settle_s >= 0.3


def test_gate_writes_status_file_for_lcd(tmp_path):
    import json
    f = tmp_path / "gate.json"
    gate.write_gate_status(str(f), "reject", now=123.0)
    assert json.loads(f.read_text()) == {"state": "reject", "ts": 123.0}
    gate.write_gate_status(str(tmp_path / "no" / "dir.json"), "open")   # 寫不進去不丟例外
    gate.write_gate_status("", "open")
def _history_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "h.db"))
    return TestClient(main.app), {"Authorization": f"Bearer {main.ADMIN_TOKEN}"}


def test_history_is_admin_only_sorted_and_windowed(tmp_path, monkeypatch):
    cl, adm = _history_client(tmp_path, monkeypatch)
    with cl:
        cl.post("/api/bins", headers=adm, json={"id": "NTNU-01", "name": "x", "lat": 25.0, "lng": 121.5})
        now = time.time()
        with db.conn() as c:
            for ts, f, d, w, b in [(now - 30, 50, 150, None, 1), (now - 2 * 3600, 10, 270, 100, 0),
                                   (now - 600, 40, 180, 300, 0)]:
                c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,dist_mm,weight_g,blocked) VALUES(?,?,?,?,?,?)",
                          ("NTNU-01", ts, f, d, w, b))
        assert cl.get("/api/bins/NTNU-01/history").status_code == 401
        h = cl.get("/api/bins/NTNU-01/history?hours=1", headers=adm).json()
        assert [p["fill_pct"] for p in h] == [40, 50]          # 2 小時前那筆在範圍外，依時間排序
        assert h[-1] == {"ts": h[-1]["ts"], "fill_pct": 50, "dist_mm": 150, "weight_g": None, "blocked": True}
        assert len(cl.get("/api/bins/NTNU-01/history?hours=3", headers=adm).json()) == 3
        assert cl.get("/api/bins/NOPE/history", headers=adm).status_code == 404
        assert cl.get("/api/bins/NTNU-01/history?hours=1000", headers=adm).status_code == 422


def test_history_downsamples_but_keeps_latest(tmp_path, monkeypatch):
    cl, adm = _history_client(tmp_path, monkeypatch)
    with cl:
        cl.post("/api/bins", headers=adm, json={"id": "NTNU-01", "name": "x", "lat": 25.0, "lng": 121.5})
        now = time.time()
        with db.conn() as c:
            c.executemany("INSERT INTO telemetry(bin_id,ts,fill_pct) VALUES(?,?,?)",
                          [("NTNU-01", now - 3000 + i, i % 100) for i in range(1000)])
        h = cl.get("/api/bins/NTNU-01/history?hours=1&max_points=50", headers=adm).json()
        assert len(h) == 50
        assert h[-1]["ts"] == now - 3000 + 999
        assert all(a["ts"] < b["ts"] for a, b in zip(h, h[1:]))


def test_admin_bins_show_latest_raw_reading_but_public_does_not(tmp_path, monkeypatch):
    cl, adm = _history_client(tmp_path, monkeypatch)
    with cl:
        tok = cl.post("/api/bins", headers=adm, json={"id": "NTNU-01", "name": "x", "lat": 25.0, "lng": 121.5}).json()["device_token"]
        cl.post("/api/bins", headers=adm, json={"id": "EMPTY", "name": "y", "lat": 25.0, "lng": 121.5})
        for body in ({"fill_pct": 20, "dist_mm": 240}, {"fill_pct": 90, "dist_mm": 30, "blocked": True}):
            cl.post("/api/telemetry", headers={"X-Device-Token": tok}, json={"bin_id": "NTNU-01", **body})
        full = {b["id"]: b for b in cl.get("/api/admin/bins", headers=adm).json()}
        assert full["NTNU-01"]["dist_mm"] == 30 and full["NTNU-01"]["blocked"] is True
        assert full["EMPTY"]["dist_mm"] is None and full["EMPTY"]["blocked"] is None
        pub = cl.get("/api/bins").json()[0]
        assert "dist_mm" not in pub and "blocked" not in pub
