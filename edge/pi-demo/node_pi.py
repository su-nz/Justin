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
import logging
import math
import os
import random
import statistics
import sys
import time

log = logging.getLogger("node_pi")

SONAR_TRIG = 5
SONAR_ECHO = 6
SONAR_MAX_M = 2.0
SONAR_SAMPLES = 9          # 每輪量 9 次取中位數
HX_DT = 22
HX_SCK = 27
HX_SAMPLES = 15            # 每輪讀 15 筆取中位數
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

    def update(self, dist_cm: float | None) -> bool:
        suspicious = dist_cm is None or dist_cm < self.min_cm
        self.count = self.count + 1 if suspicious else 0
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


def lcd_lines(bin_id: str, fill_pct: float | None, weight_g: float | None,
              blocked: bool, scale_enabled: bool = True) -> tuple[str, str]:
    """第一行 `NTNU-01  62%`，第二行重量 `1.25 kg`（遮擋時後面加 BLOCKED）。"""
    fill = "--%" if fill_pct is None else f"{fill_pct:.0f}%"
    line1 = f"{bin_id}  {fill}"
    if not scale_enabled:
        line2 = "no scale"
    elif weight_g is None:
        line2 = "-- kg"
    else:
        line2 = f"{weight_g / 1000:.2f} kg"
    if blocked:
        line2 += " BLOCKED"
    return line1[:LCD_COLS], line2[:LCD_COLS]


# ----------------------------------------------------------------------------- 硬體
class Sonar:
    """HC-SR04（gpiozero）。回傳 9 次量測的中位數（cm），量不到回傳 None。"""

    def __init__(self, trig: int = SONAR_TRIG, echo: int = SONAR_ECHO):
        from gpiozero import DistanceSensor
        # queue_len=1：關掉 gpiozero 內建的平均，自己取中位數
        self.dev = DistanceSensor(echo=echo, trigger=trig, max_distance=SONAR_MAX_M,
                                  queue_len=1, partial=True)

    def read_cm(self) -> float | None:
        vals = []
        for _ in range(SONAR_SAMPLES):
            time.sleep(0.07)  # HC-SR04 建議兩次量測間隔 > 60 ms
            d = self.dev.distance
            if d is not None and d < SONAR_MAX_M * 0.98:  # 沒回波時 gpiozero 給 max_distance
                vals.append(d * 100)
        return robust_median(vals, min_count=3)

    def close(self) -> None:
        self.dev.close()


class _LgpioPins:
    def __init__(self, dt: int, sck: int):
        import lgpio
        self.lg = lgpio
        self.h = lgpio.gpiochip_open(0)
        self.dt, self.sck = dt, sck
        lgpio.gpio_claim_output(self.h, sck, 0)
        # 上拉：DT 線沒接時讀到高電位 → 逾時，而不是讀到浮接的亂數
        lgpio.gpio_claim_input(self.h, dt, lgpio.SET_PULL_UP)

    def sck_write(self, v: int) -> None:
        self.lg.gpio_write(self.h, self.sck, v)

    def dt_read(self) -> int:
        return self.lg.gpio_read(self.h, self.dt)

    def close(self) -> None:
        self.lg.gpiochip_close(self.h)


class _RpiGpioPins:
    def __init__(self, dt: int, sck: int):
        import RPi.GPIO as GPIO
        self.GPIO = GPIO
        self.dt, self.sck = dt, sck
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(sck, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(dt, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def sck_write(self, v: int) -> None:
        self.GPIO.output(self.sck, v)

    def dt_read(self) -> int:
        return self.GPIO.input(self.dt)

    def close(self) -> None:
        self.GPIO.cleanup((self.dt, self.sck))


class HX711:
    """軟體模擬 HX711 時序，A 通道、增益 128。"""

    def __init__(self, dt: int = HX_DT, sck: int = HX_SCK, pins=None):
        if pins is None:
            try:
                pins = _LgpioPins(dt, sck)
            except ImportError:
                pins = _RpiGpioPins(dt, sck)
        self.pins = pins

    def read_raw(self, timeout: float = HX_TIMEOUT_S) -> int | None:
        p = self.pins
        deadline = time.monotonic() + timeout
        while p.dt_read():  # DT 拉低代表資料準備好
            if time.monotonic() > deadline:
                return None
            time.sleep(0.001)
        v = 0
        for _ in range(24):
            p.sck_write(1)
            v = (v << 1) | p.dt_read()
            p.sck_write(0)
        p.sck_write(1)  # 第 25 個脈波：下一筆用 A 通道、增益 128
        p.sck_write(0)
        raw = to_signed24(v)
        if raw in (0x7FFFFF, -0x800000):  # 飽和：通常是接線錯或荷重元沒接
            return None
        return raw

    def read_median(self, n: int = HX_SAMPLES) -> float | None:
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
        self.pins.close()


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
        if not enabled:
            return
        try:
            from RPLCD.i2c import CharLCD
            self.dev = CharLCD("PCF8574", addr, cols=LCD_COLS, rows=2)
            self.dev.clear()
        except Exception as e:  # 沒裝 RPLCD、沒開 I²C、LCD 沒接都不影響上傳
            log.warning("LCD 無法使用（%s），只寫 log", e)

    def show(self, line1: str, line2: str) -> None:
        log.info("LCD | %-16s | %-16s |", line1, line2)
        if self.dev is None:
            return
        try:
            self.dev.cursor_pos = (0, 0)
            self.dev.write_string(line1.ljust(LCD_COLS))
            self.dev.cursor_pos = (1, 0)
            self.dev.write_string(line2.ljust(LCD_COLS))
        except Exception as e:
            log.warning("LCD 寫入失敗：%s", e)

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close(clear=False)
            except Exception:
                pass


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


# ----------------------------------------------------------------------------- 節點主迴圈
class Node:
    def __init__(self, bin_id: str, depth_cm: float, sonar, scale=None,
                 hx_scale: float | None = None, hx_offset: float | None = None,
                 lcd: Lcd | None = None, poster=None):
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

    def read_weight(self) -> float | None:
        if self.scale is None:
            return None
        try:
            raw = self.scale.read_median()
        except Exception as e:
            log.warning("HX711 讀取錯誤：%s", e)
            return None
        if raw is None:
            log.warning("HX711 沒有回應（逾時），這輪不送 weight_g")
            return None
        return (raw - self.hx_offset) / self.hx_scale

    def step(self) -> dict | None:
        """量一輪、更新 LCD、上傳。回傳送出的 payload；沒東西可送時回傳 None。"""
        try:
            dist = self.sonar.read_cm()
        except Exception as e:
            log.warning("超音波讀取錯誤：%s", e)
            dist = None
        blocked = self.blocks.update(dist)
        if dist is not None:
            self.last_fill = fill_from_distance(dist, self.depth_cm)
        fill = self.last_fill  # 量不到時沿用上次的值，不亂報（和韌體一致）
        weight = self.read_weight()  # 超音波遮擋時照樣量重量

        if self.lcd is not None:
            self.lcd.show(*lcd_lines(self.bin_id, fill, weight, blocked, self.scale is not None))

        log.info("dist=%s cm fill=%s%% weight=%s g blocked=%s",
                 "--" if dist is None else f"{dist:.1f}",
                 "--" if fill is None else f"{fill:.1f}",
                 "--" if weight is None else f"{weight:.0f}", blocked)
        if fill is None:
            log.warning("還沒量到任何距離，後端需要 fill_pct，這輪不上傳")
            return None
        payload = build_payload(self.bin_id, fill, dist, weight, blocked)
        if self.poster is not None and self.poster(payload):
            log.info("已上傳 %s", payload)
        return payload

    def run(self, interval: float, once: bool = False) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.step()
            except Exception:  # 任何意外都不能讓節點停掉
                log.exception("這輪發生錯誤，下一輪再試")
            if once:
                return
            time.sleep(max(interval - (time.monotonic() - t0), 1.0))


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
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.hx_scale is None:
        a.hx_scale = _env_float("HX711_SCALE")
    if a.hx_offset is None:
        a.hx_offset = _env_float("HX711_OFFSET")
    if a.depth_cm <= 0:
        ap.error("--depth-cm 必須大於 0")
    return a


def _make_scale(a: argparse.Namespace, sonar):
    if a.dry:
        return FakeScale(sonar)
    return HX711(a.hx_dt, a.hx_sck)


def calibrate(a: argparse.Namespace) -> int:
    """--tare / --calibrate：印出要設的環境變數後結束。"""
    scale = _make_scale(a, FakeSonar(a.depth_cm) if a.dry else None)
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


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 成功時不用每筆都印
    if a.tare or a.calibrate is not None:
        return calibrate(a)

    if not a.token:
        log.warning("沒有 DEVICE_TOKEN，後端會回 401")

    sonar = FakeSonar(a.depth_cm) if a.dry else Sonar()
    scale = None
    hx_scale, hx_offset = a.hx_scale, a.hx_offset
    if a.dry and not a.no_scale:
        scale, hx_scale, hx_offset = FakeScale(sonar), 1.0, 0.0
    elif not a.no_scale:
        if hx_scale is None or hx_offset is None or math.isclose(hx_scale, 0.0):
            log.warning("沒有設定 HX711_SCALE / HX711_OFFSET，先用 --tare、--calibrate 校正；"
                        "這次不送 weight_g")
        else:
            try:
                scale = HX711(a.hx_dt, a.hx_sck)
            except Exception as e:
                log.warning("HX711 初始化失敗（%s），這次不送 weight_g", e)
    lcd = Lcd(a.lcd_addr, enabled=not (a.dry or a.no_lcd))

    node = Node(a.bin, a.depth_cm, sonar, scale, hx_scale, hx_offset, lcd,
                poster=lambda p: post_telemetry(a.api, a.token, p))
    log.info("開始：bin=%s api=%s 桶深=%.1f cm 間隔=%.0f s 秤重=%s%s", a.bin, a.api, a.depth_cm,
             a.interval, "開" if scale else "關", "（dry）" if a.dry else "")
    try:
        node.run(a.interval, once=a.once)
    except KeyboardInterrupt:
        pass
    finally:
        for dev in (sonar, scale, lcd):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
