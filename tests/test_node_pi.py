"""edge/pi-demo/node_pi.py 的單元測試，不需要 Pi。"""
import sys
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


class FakePins:
    """依 HX711 時序吐出預先設定的 24 位元值。"""

    def __init__(self, words, ready=True):
        self.words = list(words)
        self.ready = ready
        self.bits = []
        self.pulses = 0

    def dt_read(self):
        if not self.bits:
            if not self.ready or not self.words:
                return 1  # 沒準備好（或沒接，上拉成高電位）
            w = self.words.pop(0) & 0xFFFFFF
            self.bits = [(w >> (23 - i)) & 1 for i in range(24)]
            return 0
        return self.bits[0]

    def sck_write(self, v):
        if v:
            self.pulses += 1
        elif self.bits:
            self.bits.pop(0)

    def close(self):
        pass


def test_hx711_reads_signed_values():
    hx = node_pi.HX711(pins=FakePins([0x000123, 0xFFFF00]))
    assert hx.read_raw() == 0x123
    assert hx.read_raw() == -256
    assert hx.pins.pulses == 50  # 每筆 24 + 1 個脈波（A 通道、增益 128）


def test_hx711_timeout_returns_none():
    hx = node_pi.HX711(pins=FakePins([], ready=False))
    assert hx.read_raw(timeout=0.01) is None
    assert hx.read_median() is None


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
