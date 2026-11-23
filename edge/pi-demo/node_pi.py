#!/usr/bin/env python3
"""Demo 版感測節點（Raspberry Pi 4）：超音波量滿度 + HX711 量重量，每 30 秒上傳一次。

接線（實體腳位 / BCM）：
  HC-SR04  Trig = pin 29 / GPIO5、Echo = pin 31 / GPIO6（Echo 經分壓到 3.3 V）
  HX711    DT = pin 15 / GPIO22、SCK = pin 13 / GPIO27、VCC = pin 17（3.3 V，不能接 5 V）、GND = pin 14
  LCD1602  I²C（PCF8574，位址 0x27）
GPIO18、23、24、25 是 gate.py 的，這支程式不碰。

用法：
  python3 node_pi.py --api http://<server>:8000 --bin NTNU-01 --token <token> --depth-cm 30
  python3 node_pi.py --dry --api http://localhost:8000 --bin NTNU-01 --token <token>   # 筆電，不碰 GPIO
  python3 node_pi.py --tare                     # 空桶：印出 HX711_OFFSET
  python3 node_pi.py --calibrate 600            # 放 600 g：印出 HX711_SCALE（要先設好 offset）
  python3 node_pi.py --no-scale ...             # 沒接 HX711，只送滿度

沒給參數時讀環境變數 SMARTBIN_API、BIN_ID、DEVICE_TOKEN、HX711_SCALE、HX711_OFFSET。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

log = logging.getLogger("node_pi")

SONAR_TRIG = 5
SONAR_ECHO = 6
SONAR_MAX_M = 2.0
SONAR_SAMPLES = 9          # 每輪量 9 次取中位數
HX_DT = 22
HX_SCK = 27
HX_SAMPLES = 15            # 每輪讀 15 筆取中位數（HX711 每秒 10 筆 → 約 1.5 秒）
FAST_SONAR_SAMPLES = 5     # --fast：每輪約 0.6 秒，LCD／網站接近每秒更新（#43）
FAST_HX_SAMPLES = 4
ZERO_BAND_G = 50.0         # |重量| < 50 g 視為 0：空秤零點漂移不要顯示成有東西（#46）
HX_TARE_SAMPLES = 30
HX_TIMEOUT_S = 1.0         # HX711 10 SPS，1 秒內一定要有資料，否則視為沒接
BLOCK_DIST_CM = 2.5        # 和韌體 BLOCK_DIST_MM 一致
BLOCK_CONSECUTIVE = 3      # 連續 N 輪量不到或太近 → blocked
LCD_ADDR = 0x27
LCD_COLS = 16


# ----------------------------------------------------------------------------- 純邏輯（可單元測試）
def fill_from_distance(dist_cm: float, depth_cm: float) -> float:
    """(桶深 − 距離) / 桶深 × 100，限制在 0–100。"""
    pct = (depth_cm - dist_cm) / depth_cm * 100
    return min(max(pct, 0.0), 100.0)


def robust_median(values: list[float], k: float = 3.5, min_count: int = 1) -> float | None:
    """丟掉離中位數超過 k 倍 MAD 的異常值後再取中位數。

    Linux 用軟體模擬 HX711 時序，偶爾會被排程打斷而讀到錯位的值，這些值通常差很多。
    """
    if len(values) < min_count or not values:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    if mad == 0:
        return float(med)
    keep = [v for v in values if abs(v - med) <= k * 1.4826 * mad]
    if len(keep) < min_count:
        return None
    return float(statistics.median(keep))


def to_signed24(raw: int) -> int:
    """HX711 輸出 24 位元二補數。"""
    raw &= 0xFFFFFF
    return raw - 0x1000000 if raw & 0x800000 else raw


class BlockDetector:
    """連續 N 次量不到或距離 < 2.5 cm 才判定遮擋，避免手伸進去一下就誤報。"""

    def __init__(self, consecutive: int = BLOCK_CONSECUTIVE, min_cm: float = BLOCK_DIST_CM):
        self.consecutive = consecutive
        self.min_cm = min_cm
        self.count = 0
        self.reason: str | None = None  # 最近一次可疑的原因：no_echo | too_close（#53）

    def update(self, dist_cm: float | None) -> bool:
        if dist_cm is None:
            self.reason = "no_echo"
        elif dist_cm < self.min_cm:
            self.reason = "too_close"
        else:
            self.reason = None
        self.count = self.count + 1 if self.reason else 0
        return self.count >= self.consecutive


def build_payload(bin_id: str, fill_pct: float | None, dist_cm: float | None,
                  weight_g: float | None, blocked: bool) -> dict:
    """weight_g 是 None（沒接秤或逾時）就不送這個欄位。"""
    p: dict = {"bin_id": bin_id, "blocked": bool(blocked)}
    if fill_pct is not None:
        p["fill_pct"] = round(fill_pct, 1)
    if dist_cm is not None:
        p["dist_mm"] = int(round(dist_cm * 10))
    if weight_g is not None:
        p["weight_g"] = round(weight_g, 1)
    return p


GATE_STATUS_FILE = os.environ.get("GATE_STATUS_FILE", "/tmp/smartbin-gate.json")
GATE_LABELS = {"open": ("OPEN", 15.0), "reject": ("REJECT", 5.0)}  # 狀態 → (LCD 字樣, 顯示幾秒)
LCD_POLL_S = 0.5           # 兩輪量測之間多久檢查一次閘門狀態
COMMAND_PERIOD_S = 2.0     # 多久向後端拿一次調度台的遠端指令（#41）
LCD_TEXT_S = 10.0          # 遠端 LCD 文字顯示幾秒


def read_gate_status(path: str, now: float | None = None) -> str | None:
    """gate.py 寫的狀態檔 → LCD 字樣；檔案不存在、壞掉、過期或是關閘都回 None。"""
    if not path:
        return None
    try:
        d = json.loads(Path(path).read_text())
        label, ttl = GATE_LABELS[d["state"]]
        age = (now or time.time()) - float(d["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return label if -5 <= age <= ttl else None


def lcd_lines(bin_id: str, fill_pct: float | None, weight_g: float | None,
              blocked: bool, scale_enabled: bool = True, gate: str | None = None) -> tuple[str, str]:
    """第一行 `NTNU-01  62%`，第二行重量 `1.25 kg`，後面加擋板狀態（OPEN／REJECT），沒有時遮擋加 BLOCKED。"""
    fill = "--%" if fill_pct is None else f"{fill_pct:.0f}%"
    line1 = f"{bin_id}  {fill}"
    if not scale_enabled:
        line2 = "no scale"
    elif weight_g is None:
        line2 = "-- kg"
    else:
        line2 = f"{weight_g / 1000:.2f} kg"
    if gate:
        line2 += f" {gate}"
    elif blocked:
        line2 += " BLOCKED"
    return line1[:LCD_COLS], line2[:LCD_COLS]


# ----------------------------------------------------------------------------- 硬體
class _LgpioBackend:
    """Pi 4／Pi 5（Bookworm）預設的 GPIO 函式庫。"""

    def __init__(self):
        import lgpio
        self.lg = lgpio
        self.h = lgpio.gpiochip_open(0)

    def output(self, pin: int, value: int = 0) -> None:
        self.lg.gpio_claim_output(self.h, pin, value)

    def input(self, pin: int, pull_up: bool = False) -> None:
        self.lg.gpio_claim_input(self.h, pin, self.lg.SET_PULL_UP if pull_up else 0)

    def write(self, pin: int, value: int) -> None:
        self.lg.gpio_write(self.h, pin, value)

    def read(self, pin: int) -> int:
        return self.lg.gpio_read(self.h, pin)

    def close(self) -> None:
        self.lg.gpiochip_close(self.h)


class _RpiGpioBackend:
    """舊版系統沒有 lgpio 時退回 RPi.GPIO。"""

    def __init__(self):
        import RPi.GPIO as GPIO
        self.GPIO = GPIO
        self.pins: list[int] = []
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

    def output(self, pin: int, value: int = 0) -> None:
        self.GPIO.setup(pin, self.GPIO.OUT, initial=value)
        self.pins.append(pin)

    def input(self, pin: int, pull_up: bool = False) -> None:
        self.GPIO.setup(pin, self.GPIO.IN, pull_up_down=self.GPIO.PUD_UP if pull_up else self.GPIO.PUD_OFF)
        self.pins.append(pin)

    def write(self, pin: int, value: int) -> None:
        self.GPIO.output(pin, value)

    def read(self, pin: int) -> int:
        return self.GPIO.input(pin)

    def close(self) -> None:
        if self.pins:
            self.GPIO.cleanup(self.pins)


def open_gpio():
    try:
        return _LgpioBackend()
    except ImportError:
        return _RpiGpioBackend()


def _wait_level(gpio, pin: int, level: int, timeout: float) -> float | None:
    """等 pin 變成 level，回傳當下時間；逾時回傳 None。"""
    deadline = time.perf_counter() + timeout
    while gpio.read(pin) != level:
        if time.perf_counter() > deadline:
            return None
    return time.perf_counter()


class Sonar:
    """HC-SR04。每次都重新送 Trig、量 Echo 脈寬，沒有回波就是 None，不會沿用舊值。

    不用 gpiozero 的 DistanceSensor：它會丟掉逾時的樣本，`.distance` 繼續回傳上一次的快取，
    冷啟動從沒收到回波時還會回傳 0（被換算成 100% 滿）。
    """

    RISE_TIMEOUT_S = 0.01     # Trig 後約 0.5 ms Echo 就會拉高
    FALL_TIMEOUT_S = 0.025    # 沒回波時有些模組會拉高約 38 ms，超過就算量不到

    def __init__(self, gpio, trig: int = SONAR_TRIG, echo: int = SONAR_ECHO,
                 max_cm: float = SONAR_MAX_M * 100, samples: int = SONAR_SAMPLES):
        self.gpio, self.trig, self.echo, self.max_cm = gpio, trig, echo, max_cm
        self.samples = max(samples, 3)  # 至少 3 筆才取中位數
        gpio.output(trig, 0)
        gpio.input(echo)  # Echo 經分壓接進來，電位是確定的，不用上拉

    def ping_cm(self) -> float | None:
        g = self.gpio
        if _wait_level(g, self.echo, 0, self.FALL_TIMEOUT_S) is None:
            return None  # 上一次的 Echo 還沒結束
        g.write(self.trig, 1)
        time.sleep(0.00001)  # ≥ 10 µs 觸發脈衝
        g.write(self.trig, 0)
        t0 = _wait_level(g, self.echo, 1, self.RISE_TIMEOUT_S)
        if t0 is None:
            return None
        t1 = _wait_level(g, self.echo, 0, self.FALL_TIMEOUT_S)
        if t1 is None:
            return None
        cm = (t1 - t0) * 34300 / 2
        return cm if cm <= self.max_cm else None

    def read_cm(self) -> float | None:
        vals = []
        for i in range(self.samples):
            if i:
                time.sleep(0.06)  # HC-SR04 建議兩次量測間隔 > 60 ms
            d = self.ping_cm()
            if d is not None:
                vals.append(d)
        return robust_median(vals, min_count=3)

    def close(self) -> None:
        pass  # GPIO 由 main() 統一關閉


class HX711:
    """軟體模擬 HX711 時序，A 通道、增益 128。"""

    def __init__(self, gpio, dt: int = HX_DT, sck: int = HX_SCK, samples: int = HX_SAMPLES):
        self.gpio, self.dt, self.sck = gpio, dt, sck
        self.samples = max(samples, 3)
        gpio.output(sck, 0)
        # 上拉：DT 線沒接時讀到高電位 → 逾時，而不是讀到浮接的亂數
        gpio.input(dt, pull_up=True)

    def read_raw(self, timeout: float = HX_TIMEOUT_S) -> int | None:
        g = self.gpio
        deadline = time.monotonic() + timeout
        while g.read(self.dt):  # DT 拉低代表資料準備好
            if time.monotonic() > deadline:
                return None
            time.sleep(0.001)
        v = 0
        try:
            for _ in range(24):
                g.write(self.sck, 1)
                v = (v << 1) | g.read(self.dt)
                g.write(self.sck, 0)
            g.write(self.sck, 1)  # 第 25 個脈波：下一筆用 A 通道、增益 128
            g.write(self.sck, 0)
        finally:
            # 讀到一半出錯也要把 SCK 拉低：SCK 高電位超過 60 µs 會讓 HX711 進入省電，下一輪就讀不到
            try:
                g.write(self.sck, 0)
            except Exception:
                log.exception("HX711 SCK 拉低失敗")
        raw = to_signed24(v)
        if raw in (0x7FFFFF, -0x800000):  # 飽和：通常是接線錯或荷重元沒接
            return None
        return raw

    def read_median(self, n: int | None = None) -> float | None:
        n = n or self.samples
        vals = []
        for _ in range(n):
            r = self.read_raw()
            if r is None:
                if not vals:
                    return None  # 第一筆就逾時：當作沒接，不要卡 n 秒
                continue
            vals.append(r)
        return robust_median(vals, min_count=max(3, n // 2))

    def close(self) -> None:
        pass  # GPIO 由 main() 統一關閉


class FakeSonar:
    """--dry：垃圾慢慢堆高，偶爾抖一下。"""

    def __init__(self, depth_cm: float):
        self.depth = depth_cm
        self.dist = depth_cm * 0.9

    def read_cm(self) -> float | None:
        self.dist = max(self.dist - random.uniform(0.0, 0.6), 3.0)
        return round(self.dist + random.gauss(0, 0.2), 1)

    def close(self) -> None:
        pass


class FakeScale:
    """--dry：回傳假的原始值，搭配 offset=0、scale=1 時就是克數。"""

    def __init__(self, sonar: FakeSonar):
        self.sonar = sonar

    def read_median(self, n: int = HX_SAMPLES) -> float | None:
        level = 1 - self.sonar.dist / self.sonar.depth
        return max(level, 0) * 3000 + random.gauss(0, 5)

    def close(self) -> None:
        pass


class Lcd:
    def __init__(self, addr: int = LCD_ADDR, enabled: bool = True):
        self.dev = None
        self.state, self.error = "disabled", None   # 元件狀態（#53）：disabled | unavailable | ok | error
        if not enabled:
            return
        try:
            from RPLCD.i2c import CharLCD
            self.dev = CharLCD("PCF8574", addr, cols=LCD_COLS, rows=2)
            self.dev.clear()
            self.state = "ok"
        except Exception as e:  # 沒裝 RPLCD、沒開 I²C、LCD 沒接都不影響上傳
            log.warning("LCD 無法使用（%s），只寫 log", e)
            self.state, self.error = "unavailable", str(e)[:120]

    def show(self, line1: str, line2: str) -> None:
        log.info("LCD | %-16s | %-16s |", line1, line2)
        if self.dev is None:
            return
        try:
            self.dev.cursor_pos = (0, 0)
            self.dev.write_string(line1.ljust(LCD_COLS))
            self.dev.cursor_pos = (1, 0)
            self.dev.write_string(line2.ljust(LCD_COLS))
            self.state, self.error = "ok", None
        except Exception as e:
            log.warning("LCD 寫入失敗：%s", e)
            self.state, self.error = "error", str(e)[:120]

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close(clear=False)
            except Exception:
                pass


STATUS_HEARTBEAT_S = 10.0  # 狀態沒變也每 10 秒報一次，調度台才知道還活著


def post_status(api: str, token: str, bin_id: str, components: dict) -> bool:
    """元件狀態上報（#53）；失敗只記 debug，不影響量測。"""
    import httpx
    try:
        r = httpx.post(f"{api.rstrip('/')}/api/device/status", timeout=5,
                       json={"bin_id": bin_id, "source": "node", "components": components},
                       headers={"X-Device-Token": token})
        return r.status_code < 300
    except httpx.HTTPError as e:
        log.debug("狀態上報失敗：%s", e)
        return False


def post_telemetry(api: str, token: str, payload: dict) -> bool:
    import httpx
    try:
        r = httpx.post(f"{api.rstrip('/')}/api/telemetry", json=payload, timeout=5,
                       headers={"X-Device-Token": token})
        if r.status_code >= 300:
            log.warning("上傳失敗 HTTP %s：%s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("上傳失敗：%s", e)
        return False


class CommandClient:
    """向後端拿調度台送給 node 的指令，執行後回報（#41）。網路錯誤只記 debug，不影響量測。"""

    def __init__(self, api: str, token: str, bin_id: str):
        self.api, self.token, self.bin_id = api.rstrip("/"), token, bin_id

    def fetch(self) -> list[dict]:
        import httpx
        try:
            r = httpx.get(f"{self.api}/api/device/commands", timeout=5,
                          params={"bin_id": self.bin_id, "target": "node"},
                          headers={"X-Device-Token": self.token})
            return r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as e:
            log.debug("拿不到遠端指令：%s", e)
            return []

    def report(self, cid: int, ok: bool, result) -> None:
        import httpx
        try:
            httpx.post(f"{self.api}/api/device/commands/{cid}/result", timeout=5,
                       params={"bin_id": self.bin_id}, json={"ok": ok, "result": result},
                       headers={"X-Device-Token": self.token})
        except httpx.HTTPError as e:
            log.warning("遠端指令結果回報失敗：%s", e)


def split_lcd_text(text: str) -> tuple[str, str]:
    """自訂文字拆成兩行；有換行就照換行，否則每行 16 字。"""
    parts = text.split("\n", 1) if "\n" in text else [text[:LCD_COLS], text[LCD_COLS:]]
    return parts[0][:LCD_COLS], (parts[1] if len(parts) > 1 else "")[:LCD_COLS]


# ----------------------------------------------------------------------------- 節點主迴圈
class Node:
    def __init__(self, bin_id: str, depth_cm: float, sonar, scale=None,
                 hx_scale: float | None = None, hx_offset: float | None = None,
                 lcd: Lcd | None = None, poster=None, gate_status_file: str = "",
                 commands: CommandClient | None = None, zero_band_g: float = ZERO_BAND_G,
                 status_poster=None, scale_note: str | None = None):
        self.bin_id = bin_id
        self.depth_cm = depth_cm
        self.sonar = sonar
        self.scale = scale
        self.hx_scale = hx_scale
        self.hx_offset = hx_offset
        self.lcd = lcd
        self.poster = poster
        self.blocks = BlockDetector()
        self.last_fill: float | None = None
        self.gate_status_file = gate_status_file
        self._reading: tuple | None = None   # 最近一輪 (fill, weight, blocked)，閘門狀態變了就重畫
        self._lcd_lines: tuple[str, str] | None = None
        self.commands = commands
        self.zero_band_g = max(zero_band_g, 0.0)
        # 元件狀態（#53）
        self.status_poster = status_poster
        self.scale_note = scale_note        # 沒有秤時的原因：disabled | not_calibrated | init_failed
        self.scale_state = "ok" if scale is not None else (scale_note or "disabled")
        self.sonar_error: str | None = None
        self._status_key = None
        self._status_at = 0.0
        self.lcd_text: tuple[str, str] | None = None   # 遠端自訂文字
        self.lcd_text_until = 0.0

    def refresh_lcd(self, now: float | None = None) -> None:
        """依最近一輪讀值 + 閘門狀態更新 LCD；內容沒變就不寫 I²C。"""
        if self.lcd is None or self._reading is None:
            return
        now = now or time.time()
        fill, weight, blocked = self._reading
        if self.lcd_text and now < self.lcd_text_until:
            lines = self.lcd_text
        else:
            self.lcd_text = None
            lines = lcd_lines(self.bin_id, fill, weight, blocked, self.scale is not None,
                              read_gate_status(self.gate_status_file, now))
        if lines != self._lcd_lines:
            self.lcd.show(*lines)
            self._lcd_lines = lines

    def read_weight(self) -> float | None:
        if self.scale is None:
            return None
        try:
            raw = self.scale.read_median()
        except Exception as e:
            log.warning("HX711 讀取錯誤：%s", e)
            self.scale_state = "error"
            return None
        if raw is None:
            log.warning("HX711 沒有回應（逾時），這輪不送 weight_g")
            self.scale_state = "timeout"
            return None
        self.scale_state = "ok"
        grams = (raw - self.hx_offset) / self.hx_scale
        return 0.0 if abs(grams) < self.zero_band_g else grams

    def step(self) -> dict | None:
        """量一輪、更新 LCD、上傳。回傳送出的 payload；沒東西可送時回傳 None。"""
        try:
            dist = self.sonar.read_cm()
            self.sonar_error = None
        except Exception as e:
            log.warning("超音波讀取錯誤：%s", e)
            dist = None
            self.sonar_error = str(e)[:120]
        blocked = self.blocks.update(dist)
        if dist is not None:
            self.last_fill = fill_from_distance(dist, self.depth_cm)
        fill = self.last_fill  # 量不到時沿用上次的值，不亂報（和韌體一致）
        weight = self.read_weight()  # 超音波遮擋時照樣量重量

        self._reading = (fill, weight, blocked)
        self.report_status(dist, weight, blocked)
        self._lcd_lines = None  # 每輪都重寫一次，LCD 接觸不良後也能恢復
        self.refresh_lcd()

        log.info("dist=%s cm fill=%s%% weight=%s g blocked=%s",
                 "--" if dist is None else f"{dist:.1f}",
                 "--" if fill is None else f"{fill:.1f}",
                 "--" if weight is None else f"{weight:.0f}", blocked)
        if fill is None:
            if not blocked:
                log.warning("還沒量到任何距離，後端需要 fill_pct，這輪不上傳")
                return None
            # 開機後一直沒有回波 → 已判定遮擋，一定要讓後端知道。後端需要 fill_pct，
            # 照韌體的做法送 0；地圖看的是 blocked（顯示維護中），不會當成空桶。
            log.warning("開機後一直量不到距離，判定遮擋；滿度未知，先送 0%")
            fill = 0.0
        payload = build_payload(self.bin_id, fill, dist, weight, blocked)
        if self.poster is not None and self.poster(payload):
            log.info("已上傳 %s", payload)
        return payload

    def components(self, dist: float | None, weight: float | None, blocked: bool) -> dict:
        b = self.blocks
        sonar_state = "error" if self.sonar_error else (b.reason or "ok")
        return {
            "sonar": {"state": sonar_state, "dist_cm": None if dist is None else round(dist, 1),
                      "streak": b.count, "need": b.consecutive, "blocked": bool(blocked),
                      "min_cm": b.min_cm, "error": self.sonar_error},
            "scale": {"state": self.scale_state,
                      "weight_g": None if weight is None else round(weight, 1)},
            "lcd": {"state": self.lcd.state if self.lcd is not None else "disabled",
                    "error": getattr(self.lcd, "error", None)},
            "node": {"depth_cm": self.depth_cm},
        }

    def report_status(self, dist, weight, blocked, now: float | None = None) -> None:
        """狀態有變（或每 10 秒）才上報，不要每輪都多打一次 API。"""
        if self.status_poster is None:
            return
        now = now or time.monotonic()
        comp = self.components(dist, weight, blocked)
        key = (comp["sonar"]["state"], comp["sonar"]["streak"] if comp["sonar"]["state"] != "ok" else 0,
               blocked, comp["scale"]["state"], comp["lcd"]["state"])
        if key == self._status_key and now - self._status_at < STATUS_HEARTBEAT_S:
            return
        self._status_key, self._status_at = key, now
        try:
            self.status_poster(comp)
        except Exception:
            log.exception("狀態上報失敗")

    def handle_command(self, cmd: str, args: dict, now: float | None = None) -> tuple[bool, str]:
        now = now or time.time()
        if cmd == "lcd_text":
            text = str(args.get("text", ""))
            if not text:
                return False, "沒有文字"
            self.lcd_text, self.lcd_text_until = split_lcd_text(text), now + LCD_TEXT_S
            self.refresh_lcd(now)
            return True, f"LCD 顯示 {LCD_TEXT_S:g} 秒"
        if cmd == "measure_now":
            p = self.step()
            if p is None:
                return False, "量不到距離，這次沒有上傳"
            w = p.get("weight_g")
            return True, f"已量測並上傳：滿度 {p['fill_pct']:.0f}%" + ("" if w is None else f"、重量 {w:.0f} g")
        if cmd == "tare":
            if self.scale is None or self.hx_scale is None:
                return False, "沒有接秤或還沒校正"
            raw = self.scale.read_median(HX_SAMPLES)  # 歸零要準，用完整取樣數
            if raw is None:
                return False, "HX711 沒有回應"
            self.hx_offset = raw
            log.info("遠端歸零：HX711_OFFSET=%.0f（重開程式後要更新 node.env 才會保留）", raw)
            return True, f"已歸零，新的 HX711_OFFSET={raw:.0f}（要長期保留請更新 node.env）"
        return False, f"感測節點不支援指令 {cmd}"

    def run_commands(self) -> None:
        if self.commands is None:
            return
        for c in self.commands.fetch():
            try:
                ok, result = self.handle_command(c["cmd"], c.get("args") or {})
            except Exception as e:  # 指令失敗不能讓節點停掉
                log.exception("遠端指令 %s 失敗", c.get("cmd"))
                ok, result = False, f"執行錯誤：{e}"
            log.info("REMOTE %s → %s", c["cmd"], result)
            self.commands.report(c["id"], ok, result)

    def run(self, interval: float, once: bool = False) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.step()
            except Exception:  # 任何意外都不能讓節點停掉
                log.exception("這輪發生錯誤，下一輪再試")
            if once:
                return
            end = max(t0 + interval, time.monotonic() + 1.0)  # 至少等 1 秒
            next_cmd = 0.0
            while (left := end - time.monotonic()) > 0:
                time.sleep(min(LCD_POLL_S, left))
                if time.monotonic() >= next_cmd:
                    next_cmd = time.monotonic() + COMMAND_PERIOD_S
                    self.run_commands()
                try:
                    self.refresh_lcd()
                except Exception:
                    log.exception("LCD 更新失敗")


# ----------------------------------------------------------------------------- CLI
def _env_float(name: str) -> float | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        log.warning("環境變數 %s=%r 不是數字，忽略", name, v)
        return None


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Smart Bin Demo 感測節點（Pi 4）")
    ap.add_argument("--api", default=os.environ.get("SMARTBIN_API", "http://localhost:8000"))
    ap.add_argument("--bin", default=os.environ.get("BIN_ID", "NTNU-01"))
    ap.add_argument("--token", default=os.environ.get("DEVICE_TOKEN", ""))
    ap.add_argument("--depth-cm", type=float, default=30.0, help="感測器到桶底的距離（cm）")
    ap.add_argument("--interval", type=float, default=30.0, help="上傳間隔（秒）")
    ap.add_argument("--once", action="store_true", help="只量一輪就結束")
    ap.add_argument("--dry", action="store_true", help="不碰 GPIO，用假距離和假重量")
    ap.add_argument("--no-scale", action="store_true", help="沒接 HX711，不送 weight_g")
    ap.add_argument("--no-lcd", action="store_true", help="不使用 LCD")
    ap.add_argument("--lcd-addr", type=lambda s: int(s, 0), default=LCD_ADDR)
    ap.add_argument("--hx-dt", type=int, default=HX_DT)
    ap.add_argument("--hx-sck", type=int, default=HX_SCK)
    ap.add_argument("--hx-scale", type=float, default=None, help="或環境變數 HX711_SCALE")
    ap.add_argument("--hx-offset", type=float, default=None, help="或環境變數 HX711_OFFSET")
    ap.add_argument("--tare", action="store_true", help="量空桶，印出 offset 後結束")
    ap.add_argument("--calibrate", type=float, metavar="GRAMS",
                    help="放已知重量（克），印出 scale 後結束")
    ap.add_argument("--sonar-samples", type=int, default=None, help=f"每輪超音波取樣數（預設 {SONAR_SAMPLES}）")
    ap.add_argument("--hx-samples", type=int, default=None, help=f"每輪 HX711 取樣數（預設 {HX_SAMPLES}）")
    ap.add_argument("--fast", action="store_true",
                    help=f"快速模式：超音波 {FAST_SONAR_SAMPLES} 筆、HX711 {FAST_HX_SAMPLES} 筆，每輪約 0.6 秒；搭配 --interval 1")
    ap.add_argument("--zero-band-g", type=float, default=None,
                    help=f"重量絕對值小於這個克數就當 0（預設 {ZERO_BAND_G:g}，或環境變數 HX711_ZERO_BAND_G；0 = 停用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.zero_band_g is None:
        env = _env_float("HX711_ZERO_BAND_G")
        a.zero_band_g = ZERO_BAND_G if env is None else env
    if a.hx_scale is None:
        a.hx_scale = _env_float("HX711_SCALE")
    if a.hx_offset is None:
        a.hx_offset = _env_float("HX711_OFFSET")
    if a.depth_cm <= 0:
        ap.error("--depth-cm 必須大於 0")
    return a


def sample_counts(a: argparse.Namespace) -> tuple[int, int]:
    """(超音波, HX711) 每輪取樣數：明確指定 > --fast > 預設。"""
    sonar = a.sonar_samples or (FAST_SONAR_SAMPLES if a.fast else SONAR_SAMPLES)
    hx = a.hx_samples or (FAST_HX_SAMPLES if a.fast else HX_SAMPLES)
    return sonar, hx


def _make_scale(a: argparse.Namespace, sonar, gpio):
    if a.dry:
        return FakeScale(sonar)
    return HX711(gpio, a.hx_dt, a.hx_sck)


def calibrate(a: argparse.Namespace) -> int:
    """--tare / --calibrate：印出要設的環境變數後結束。"""
    gpio = None if a.dry else open_gpio()
    scale = _make_scale(a, FakeSonar(a.depth_cm) if a.dry else None, gpio)
    try:
        if a.tare:
            print("量測空桶中，請不要碰桶子…")
            raw = scale.read_median(HX_TARE_SAMPLES)
            if raw is None:
                print("讀不到 HX711：檢查 VCC 接 3.3 V、DT=GPIO%d、SCK=GPIO%d" % (a.hx_dt, a.hx_sck))
                return 1
            print(f"export HX711_OFFSET={raw:.0f}")
            return 0
        if a.hx_offset is None:
            print("請先跑 --tare，再用 --hx-offset 或 HX711_OFFSET 帶入 offset")
            return 1
        if a.calibrate <= 0:
            print("--calibrate 的克數必須大於 0")
            return 1
        print(f"量測 {a.calibrate:g} g 中，請不要碰桶子…")
        raw = scale.read_median(HX_TARE_SAMPLES)
        if raw is None:
            print("讀不到 HX711")
            return 1
        k = (raw - a.hx_offset) / a.calibrate
        if abs(k) < 1e-6:
            print("讀值和空桶一樣，確認重物有放上去、荷重元接線正確")
            return 1
        print(f"export HX711_SCALE={k:.4f}")
        return 0
    finally:
        scale.close()
        if gpio is not None:
            gpio.close()


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 成功時不用每筆都印
    if a.tare or a.calibrate is not None:
        return calibrate(a)

    if not a.token:
        log.warning("沒有 DEVICE_TOKEN，後端會回 401")

    gpio = None if a.dry else open_gpio()
    sonar_n, hx_n = sample_counts(a)
    sonar = FakeSonar(a.depth_cm) if a.dry else Sonar(gpio, samples=sonar_n)
    scale = None
    scale_note = "disabled"
    hx_scale, hx_offset = a.hx_scale, a.hx_offset
    if a.dry and not a.no_scale:
        scale, hx_scale, hx_offset = FakeScale(sonar), 1.0, 0.0
    elif not a.no_scale:
        if hx_scale is None or hx_offset is None or math.isclose(hx_scale, 0.0):
            log.warning("沒有設定 HX711_SCALE / HX711_OFFSET，先用 --tare、--calibrate 校正；"
                        "這次不送 weight_g")
            scale_note = "not_calibrated"
        else:
            try:
                scale = HX711(gpio, a.hx_dt, a.hx_sck, samples=hx_n)
            except Exception as e:
                log.warning("HX711 初始化失敗（%s），這次不送 weight_g", e)
                scale_note = "init_failed"
    lcd = Lcd(a.lcd_addr, enabled=not (a.dry or a.no_lcd))

    node = Node(a.bin, a.depth_cm, sonar, scale, hx_scale, hx_offset, lcd,
                poster=lambda p: post_telemetry(a.api, a.token, p),
                gate_status_file="" if a.dry else GATE_STATUS_FILE,
                commands=CommandClient(a.api, a.token, a.bin) if a.token else None,
                zero_band_g=a.zero_band_g, scale_note=scale_note,
                status_poster=(lambda comp: post_status(a.api, a.token, a.bin, comp)) if a.token else None)
    log.info("開始：bin=%s api=%s 桶深=%.1f cm 間隔=%.0f s 秤重=%s%s", a.bin, a.api, a.depth_cm,
             a.interval, "開" if scale else "關", "（dry）" if a.dry else "")
    try:
        node.run(a.interval, once=a.once)
    except KeyboardInterrupt:
        pass
    finally:
        for dev in (sonar, scale, lcd, gpio):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
