"""edge/pi-demo/node_pi.py 的單元測試，不需要 Pi。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge" / "pi-demo"))

import node_pi  # noqa: E402


def test_fill_from_distance_clamped():
    assert node_pi.fill_from_distance(30, 30) == 0
    assert node_pi.fill_from_distance(15, 30) == 50
    assert node_pi.fill_from_distance(40, 30) == 0      # 比桶深還遠
    assert node_pi.fill_from_distance(-1, 30) == 100


def test_robust_median_drops_outliers():
    vals = [1000, 1002, 998, 1001, 999, 1003, 997, 9_000_000, -5_000_000]
    assert abs(node_pi.robust_median(vals) - 1000) <= 1
    assert node_pi.robust_median([]) is None
    assert node_pi.robust_median([5, 6], min_count=3) is None


def test_to_signed24():
    assert node_pi.to_signed24(0x000001) == 1
    assert node_pi.to_signed24(0xFFFFFF) == -1
    assert node_pi.to_signed24(0x800000) == -0x800000


def test_block_detector_needs_three_in_a_row():
    b = node_pi.BlockDetector()
    assert [b.update(d) for d in [None, 1.0, 20.0, None, None, 2.0, 15.0]] == \
        [False, False, False, False, False, True, False]


def test_payload_omits_missing_weight():
    p = node_pi.build_payload("NTNU-01", 62.345, 11.34, None, False)
    assert p == {"bin_id": "NTNU-01", "blocked": False, "fill_pct": 62.3, "dist_mm": 113}
    p = node_pi.build_payload("NTNU-01", 62.3, None, 1250.04, True)
    assert p["weight_g"] == 1250.0 and p["blocked"] is True and "dist_mm" not in p


def test_lcd_lines():
    assert node_pi.lcd_lines("NTNU-01", 62.4, 1250, False) == ("NTNU-01  62%", "1.25 kg")
    assert node_pi.lcd_lines("NTNU-01", None, None, True) == ("NTNU-01  --%", "-- kg BLOCKED")
    assert node_pi.lcd_lines("NTNU-01", 5, None, False, scale_enabled=False)[1] == "no scale"
    assert all(len(s) <= 16 for s in node_pi.lcd_lines("A-VERY-LONG-BIN-ID", 100, 99999, True))


class FakeHXGpio:
    """依 HX711 時序吐出預先設定的 24 位元值；可以指定第幾次讀 DT 時丟出例外。"""

    def __init__(self, words, ready=True, fail_on_read=None, dt=22, sck=27):
        self.words = list(words)
        self.ready = ready
        self.fail_on_read = fail_on_read
        self.dt, self.sck = dt, sck
        self.bits = []
        self.pulses = 0
        self.level = {}
        self.reads = 0

    def output(self, pin, value=0):
        self.level[pin] = value

    def input(self, pin, pull_up=False):
        pass

    def read(self, pin):
        assert pin == self.dt
        self.reads += 1
        if self.fail_on_read == self.reads:
            raise OSError("GPIO read failed")
        if not self.bits:
            if not self.ready or not self.words:
                return 1  # 沒準備好（或沒接，上拉成高電位）
            w = self.words.pop(0) & 0xFFFFFF
            self.bits = [(w >> (23 - i)) & 1 for i in range(24)]
            return 0
        return self.bits[0]

    def write(self, pin, value):
        assert pin == self.sck
        if value and not self.level.get(pin):
            self.pulses += 1
        elif not value and self.level.get(pin) and self.bits:
            self.bits.pop(0)
        self.level[pin] = value

    def close(self):
        pass


def test_hx711_reads_signed_values():
    g = FakeHXGpio([0x000123, 0xFFFF00])
    hx = node_pi.HX711(g)
    assert hx.read_raw() == 0x123
    assert hx.read_raw() == -256
    assert g.pulses == 50  # 每筆 24 + 1 個脈波（A 通道、增益 128）
    assert g.level[27] == 0


def test_hx711_timeout_returns_none():
    hx = node_pi.HX711(FakeHXGpio([], ready=False))
    assert hx.read_raw(timeout=0.01) is None
    assert hx.read_median() is None


def test_hx711_read_error_leaves_sck_low_and_recovers():
    # 第 1 次讀 DT 是「資料就緒」，第 6 次是讀第 5 個位元，這時 SCK 是高電位
    g = FakeHXGpio([0x000123, 0x000456], fail_on_read=6)
    hx = node_pi.HX711(g)
    try:
        hx.read_raw()
        assert False, "應該要把 GPIO 錯誤往上丟"
    except OSError:
        pass
    assert g.level[27] == 0  # SCK 一定要回到低電位，HX711 才不會進入省電
    g.bits = []              # 模擬 HX711 丟棄讀到一半的資料
    assert hx.read_raw() == 0x456


class FakeEchoGpio:
    """模擬 HC-SR04：Trig 下降緣後 0.2 ms 拉高 Echo，維持指定脈寬；None 代表這次沒有回波。"""

    def __init__(self, pulses, trig=5, echo=6):
        self.pulses = list(pulses)
        self.trig, self.echo = trig, echo
        self.start = self.width = None

    def output(self, pin, value=0):
        pass

    def input(self, pin, pull_up=False):
        pass

    def write(self, pin, value):
        assert pin == self.trig
        if value == 0:
            w = self.pulses.pop(0) if self.pulses else None
            self.start = time.perf_counter() + 0.0002
            self.width = w

    def read(self, pin):
        assert pin == self.echo
        if self.width is None or self.start is None:
            return 0
        t = time.perf_counter()
        return 1 if self.start <= t < self.start + self.width else 0

    def close(self):
        pass


def _echo_s(cm):
    return cm * 2 / 34300


def test_sonar_measures_fresh_distance(monkeypatch):
    monkeypatch.setattr(node_pi.time, "sleep", lambda s: None)
    g = FakeEchoGpio([_echo_s(15)] * 9)
    d = node_pi.Sonar(g).read_cm()
    assert d is not None and abs(d - 15) < 0.5


def test_sonar_cold_start_without_echo_is_none_not_zero(monkeypatch):
    monkeypatch.setattr(node_pi.time, "sleep", lambda s: None)
    monkeypatch.setattr(node_pi.Sonar, "RISE_TIMEOUT_S", 0.002)
    assert node_pi.Sonar(FakeEchoGpio([None] * 9)).read_cm() is None


def test_sonar_lost_echo_after_good_reading_is_none(monkeypatch):
    monkeypatch.setattr(node_pi.time, "sleep", lambda s: None)
    monkeypatch.setattr(node_pi.Sonar, "RISE_TIMEOUT_S", 0.002)
    s = node_pi.Sonar(FakeEchoGpio([_echo_s(15)] * 9 + [None] * 9))
    assert abs(s.read_cm() - 15) < 0.5
    assert s.read_cm() is None  # 不能回傳上一輪的 15 cm


def test_sonar_overlong_echo_counts_as_no_echo(monkeypatch):
    monkeypatch.setattr(node_pi.time, "sleep", lambda s: None)
    monkeypatch.setattr(node_pi.Sonar, "FALL_TIMEOUT_S", 0.015)
    # 有些模組沒有回波時會拉高約 38 ms；超過 2 m 的脈寬也不算數
    assert node_pi.Sonar(FakeEchoGpio([0.03] * 9)).read_cm() is None
    assert node_pi.Sonar(FakeEchoGpio([_echo_s(250)] * 9)).read_cm() is None


def test_node_reports_blocked_after_losing_echo(monkeypatch):
    monkeypatch.setattr(node_pi.time, "sleep", lambda s: None)
    monkeypatch.setattr(node_pi.Sonar, "RISE_TIMEOUT_S", 0.002)
    sonar = node_pi.Sonar(FakeEchoGpio([_echo_s(15)] * 9 + [None] * 27))
    sent = []
    node = node_pi.Node("NTNU-01", 30, sonar, poster=lambda p: sent.append(p) or True)
    for _ in range(4):
        node.step()
    assert sent[0]["dist_mm"] == 150 and not sent[0]["blocked"]
    assert all("dist_mm" not in p for p in sent[1:])   # 沒有新量測就不送距離
    assert [p["blocked"] for p in sent] == [False, False, False, True]


def test_node_cold_start_blocked_still_uploads():
    sent = []
    node = node_pi.Node("NTNU-01", 30, StubSonar([None, None, None]),
                        poster=lambda p: sent.append(p) or True)
    assert node.step() is None and node.step() is None
    p = node.step()  # 第 3 輪判定遮擋：一定要通知後端
    assert p == {"bin_id": "NTNU-01", "blocked": True, "fill_pct": 0.0} and sent == [p]


class StubSonar:
    def __init__(self, seq):
        self.seq = list(seq)

    def read_cm(self):
        v = self.seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


class StubScale:
    def __init__(self, seq):
        self.seq = list(seq)

    def read_median(self, n=15):
        v = self.seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


def test_node_step_fuses_and_survives_scale_failure():
    sent = []
    node = node_pi.Node("NTNU-01", 30, StubSonar([15.0, None, None, None]),
                        StubScale([8400 + 600 * 20, None, OSError("io"), 8400]),
                        hx_scale=20, hx_offset=8400, poster=lambda p: sent.append(p) or True)
    p1 = node.step()
    assert p1["fill_pct"] == 50 and p1["weight_g"] == 600 and not p1["blocked"]
    p2 = node.step()  # 超音波量不到：沿用上次滿度；HX711 逾時：不送 weight_g
    assert p2["fill_pct"] == 50 and "weight_g" not in p2 and "dist_mm" not in p2
    p3 = node.step()  # HX711 丟例外也不會掛
    assert "weight_g" not in p3
    p4 = node.step()  # 連續 3 輪量不到 → blocked，重量照送
    assert p4["blocked"] is True and p4["weight_g"] == 0
    assert len(sent) == 4


def test_node_skips_upload_before_first_distance():
    sent = []
    node = node_pi.Node("NTNU-01", 30, StubSonar([None]), poster=sent.append)
    assert node.step() is None and sent == []


def test_node_run_once_survives_sonar_exception():
    sent = []
    node = node_pi.Node("NTNU-01", 30, StubSonar([RuntimeError("gpio")]), poster=sent.append)
    node.run(interval=30, once=True)
    assert sent == []


def test_cli_env_and_calibration(monkeypatch, capsys):
    monkeypatch.setenv("SMARTBIN_API", "http://example:8000")
    monkeypatch.setenv("HX711_SCALE", "21.5")
    monkeypatch.setenv("HX711_OFFSET", "8400")
    a = node_pi.parse_args([])
    assert a.api == "http://example:8000" and a.hx_scale == 21.5 and a.hx_offset == 8400
    assert node_pi.parse_args(["--hx-scale", "3"]).hx_scale == 3

    monkeypatch.delenv("HX711_OFFSET")
    assert node_pi.main(["--dry", "--calibrate", "600"]) == 1   # 沒有 offset 要先 tare
    assert node_pi.main(["--dry", "--tare"]) == 0
    assert "export HX711_OFFSET=" in capsys.readouterr().out


def test_lcd_shows_gate_state_before_blocked():
    assert node_pi.lcd_lines("NTNU-01", 62, 1250, False, gate="OPEN")[1] == "1.25 kg OPEN"
    assert node_pi.lcd_lines("NTNU-01", 62, 1250, True, gate="REJECT")[1] == "1.25 kg REJECT"
    assert node_pi.lcd_lines("NTNU-01", 62, 1250, True, gate=None)[1] == "1.25 kg BLOCKED"
    assert len(node_pi.lcd_lines("NTNU-01", 62, 99999, True, gate="REJECT")[1]) <= 16


def test_read_gate_status_expires_and_ignores_close(tmp_path):
    import json
    f = tmp_path / "g.json"
    assert node_pi.read_gate_status(str(f), now=100) is None            # 沒有檔案
    f.write_text(json.dumps({"state": "open", "ts": 100}))
    assert node_pi.read_gate_status(str(f), now=110) == "OPEN"
    assert node_pi.read_gate_status(str(f), now=200) is None            # 過期
    f.write_text(json.dumps({"state": "reject", "ts": 100}))
    assert node_pi.read_gate_status(str(f), now=103) == "REJECT"
    assert node_pi.read_gate_status(str(f), now=106) is None
    f.write_text(json.dumps({"state": "close", "ts": 100}))
    assert node_pi.read_gate_status(str(f), now=101) is None
    f.write_text("{壞掉")
    assert node_pi.read_gate_status(str(f), now=101) is None
    assert node_pi.read_gate_status("", now=101) is None


def test_node_redraws_lcd_only_when_gate_state_changes(tmp_path):
    import json
    shown = []
    lcd = type("L", (), {"show": lambda self, a, b: shown.append(b)})()
    sonar = type("S", (), {"read_cm": lambda self: 20.0})()
    f = tmp_path / "g.json"
    node = node_pi.Node("NTNU-01", 30, sonar, lcd=lcd, gate_status_file=str(f))
    node.step()
    node.refresh_lcd()                       # 沒變化 → 不重寫 I²C
    assert shown == ["no scale"]
    f.write_text(json.dumps({"state": "open", "ts": node_pi.time.time()}))
    node.refresh_lcd()
    node.refresh_lcd()
    assert shown == ["no scale", "no scale OPEN"]


def test_node_remote_commands():
    shown, reports = [], []
    lcd = type("L", (), {"show": lambda self, a, b: shown.append((a, b))})()
    sonar = type("S", (), {"read_cm": lambda self: 20.0})()
    scale = type("H", (), {"read_median": lambda self, n=15: 5000.0})()
    posted = []
    cmds = [{"id": 1, "cmd": "lcd_text", "args": {"text": "Hello Professor!Welcome"}},
            {"id": 2, "cmd": "measure_now", "args": {}},
            {"id": 3, "cmd": "tare", "args": {}},
            {"id": 4, "cmd": "gate_open", "args": {}}]
    client = type("C", (), {"fetch": lambda self: cmds, "report": lambda self, *a: reports.append(a)})()
    node = node_pi.Node("NTNU-01", 30, sonar, scale, hx_scale=2.0, hx_offset=1000.0, lcd=lcd,
                        poster=lambda p: posted.append(p) or True, commands=client)
    node.run_commands()
    assert ("Hello Professor!", "Welcome") in shown
    assert [r[1] for r in reports] == [True, True, True, False]
    assert posted and posted[0]["weight_g"] == 2000.0       # (5000-1000)/2
    assert node.hx_offset == 5000.0 and node.read_weight() == 0.0
    # 自訂文字顯示期間不被量測結果蓋掉，過期後恢復
    now = node_pi.time.time()
    node.refresh_lcd(now + 1)
    assert node._lcd_lines == ("Hello Professor!", "Welcome")
    node.refresh_lcd(now + node_pi.LCD_TEXT_S + 1)
    assert node._lcd_lines[0].startswith("NTNU-01")


def test_split_lcd_text():
    assert node_pi.split_lcd_text("a\nb") == ("a", "b")
    assert node_pi.split_lcd_text("x" * 40) == ("x" * 16, "x" * 16)


def test_sample_counts_fast_and_override():
    a = node_pi.parse_args([])
    assert node_pi.sample_counts(a) == (node_pi.SONAR_SAMPLES, node_pi.HX_SAMPLES)
    assert node_pi.sample_counts(node_pi.parse_args(["--fast"])) == (5, 4)
    assert node_pi.sample_counts(node_pi.parse_args(["--fast", "--hx-samples", "7"])) == (5, 7)


def test_hx711_uses_configured_sample_count(monkeypatch):
    hx = node_pi.HX711.__new__(node_pi.HX711)
    hx.samples = 4
    calls = []
    monkeypatch.setattr(hx, "read_raw", lambda: calls.append(1) or 100, raising=False)
    assert hx.read_median() == 100 and len(calls) == 4
    calls.clear()
    hx.read_median(15)
    assert len(calls) == 15


def test_sonar_uses_configured_sample_count(monkeypatch):
    s = node_pi.Sonar.__new__(node_pi.Sonar)
    s.samples = 5
    pings = []
    monkeypatch.setattr(s, "ping_cm", lambda: pings.append(1) or 20.0, raising=False)
    monkeypatch.setattr(node_pi.time, "sleep", lambda _: None)
    assert s.read_cm() == 20.0 and len(pings) == 5


def test_weight_zero_band(monkeypatch):
    raws = iter([1040.0, 960.0, 1200.0, None])            # offset 1000、scale 1 → +40、-40、+200、讀不到
    scale = type("H", (), {"read_median": lambda self, n=None: next(raws)})()
    sonar = type("S", (), {"read_cm": lambda self: 20.0})()
    node = node_pi.Node("NTNU-01", 30, sonar, scale, hx_scale=1.0, hx_offset=1000.0)
    assert node.read_weight() == 0.0
    assert node.read_weight() == 0.0
    assert node.read_weight() == 200.0
    assert node.read_weight() is None                      # 讀不到仍是 None，不能變成 0
    raws = iter([1040.0])
    node.zero_band_g = 0                                    # 停用
    assert node.read_weight() == 40.0


def test_zero_band_cli_and_env(monkeypatch):
    monkeypatch.delenv("HX711_ZERO_BAND_G", raising=False)
    assert node_pi.parse_args([]).zero_band_g == 50.0
    monkeypatch.setenv("HX711_ZERO_BAND_G", "20")
    assert node_pi.parse_args([]).zero_band_g == 20.0
    assert node_pi.parse_args(["--zero-band-g", "0"]).zero_band_g == 0.0
