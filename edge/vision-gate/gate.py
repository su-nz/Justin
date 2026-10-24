"""邊緣視覺閘門（Edge Vision Shutter）+ 地面傾倒監看。

硬體：Raspberry Pi Zero 2 W + Raspberry Pi AI Camera（Sony IMX500，推論在感光元件上做，Pi 本身幾乎不吃算力）
      + 伺服馬達驅動投入口擋板 + 紅綠 LED。攝影機架在桶上方往下看，一個畫面切兩個 ROI：
      - slot ROI：投入口。小型隨手垃圾 → 開閘；家用垃圾袋 / 過大物體 → 不開、亮紅燈、回報 bag_rejected
      - ground ROI：桶邊地面。垃圾覆蓋率連續超標 → 回報 ground_waste（後端再用液位判斷 overflow 或 illegal_dumping）

隱私：只上傳事件（類別、信心值、覆蓋率），不上傳影像。

偵測後端：
  --backend imx500  實機（Pi + AI Camera），模型為 Ultralytics 匯出的 IMX500 套件（.rpk）
  --backend onnx    USB 攝影機 + ONNX Runtime（Pi 5 / 筆電開發用）
  --backend mock    讀 JSON 情境檔，不需硬體，用來測閘門邏輯
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gate")


# ----------------------------------------------------------------------------- config
@dataclass
class Cfg:
    api: str = os.environ.get("SMARTBIN_API", "http://localhost:8000")
    bin_id: str = os.environ.get("BIN_ID", "NTNU-01")
    token: str = os.environ.get("DEVICE_TOKEN", "")
    labels: tuple = ("cup", "bottle", "can", "paper", "lunchbox", "garbage_bag", "hand", "litter")
    small: tuple = ("cup", "bottle", "can", "paper", "lunchbox")  # 允許投入
    bag: tuple = ("garbage_bag",)                                 # 一律拒收
    no_close: tuple = ("hand",)                                   # 防夾：有手不關閘
    ground_waste: tuple = ("garbage_bag", "litter", "cup", "bottle", "can", "lunchbox", "paper")
    slot_roi: tuple = (0.35, 0.05, 0.65, 0.35)    # x0,y0,x1,y1（正規化）
    ground_roi: tuple = (0.0, 0.45, 1.0, 1.0)
    conf: float = 0.5
    max_item_area: float = 0.35     # 物體佔投入口 ROI 面積比超過 → 視為過大
    confirm_frames: int = 2         # 連續 N 幀一致才動作（Challenge 3：假警報）
    open_s: float = 4.0
    ground_period_s: float = 60.0
    ground_cov_thr: float = 0.05
    ground_confirm: int = 2
    reject_cooldown_s: float = 60.0
    ground_cooldown_s: float = 900.0
    servo_pin: int = 18
    led_green: int = 23
    led_red: int = 24
    spool: str = os.environ.get("GATE_SPOOL", "/var/lib/smartbin/spool.jsonl")


# ----------------------------------------------------------------------------- geometry
def inside(box, roi) -> float:
    """box 與 roi 交集面積 / box 面積。box、roi 皆為正規化 (x0,y0,x1,y1)。"""
    ix0, iy0 = max(box[0], roi[0]), max(box[1], roi[1])
    ix1, iy1 = min(box[2], roi[2]), min(box[3], roi[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    area = max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))
    return inter / area


def area_ratio(box, roi) -> float:
    return ((box[2] - box[0]) * (box[3] - box[1])) / ((roi[2] - roi[0]) * (roi[3] - roi[1]))


def coverage(boxes, roi, grid=48) -> float:
    """以格點近似多個框聯集佔 ROI 的比例（框會重疊，不能直接相加）。"""
    hit = 0
    for gy in range(grid):
        y = roi[1] + (gy + .5) / grid * (roi[3] - roi[1])
        for gx in range(grid):
            x = roi[0] + (gx + .5) / grid * (roi[2] - roi[0])
            if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes):
                hit += 1
    return hit / (grid * grid)


# ----------------------------------------------------------------------------- detectors
class MockDetector:
    """情境檔：[{"t": 0, "dets": [["cup", 0.9, [x0,y0,x1,y1]], ...]}, ...]，每筆是一幀。"""

    def __init__(self, path):
        self.frames = json.loads(Path(path).read_text())
        self.i = 0

    def detect(self):
        if self.i >= len(self.frames):
            return None
        f = self.frames[self.i]
        self.i += 1
        return [(d[0], d[1], tuple(d[2])) for d in f["dets"]]


class IMX500Detector:
    """Pi AI Camera。推論在 IMX500 上執行，這裡只讀出 tensor 並換算座標。"""

    def __init__(self, model_path, labels, conf):
        from picamera2 import Picamera2
        from picamera2.devices import IMX500
        self.imx = IMX500(model_path)
        self.intr = self.imx.network_intrinsics
        self.cam = Picamera2(self.imx.camera_num)
        cfg = self.cam.create_preview_configuration(controls={"FrameRate": 10}, buffer_count=6)
        self.cam.start(cfg)
        self.size = self.cam.camera_configuration()["main"]["size"]
        self.labels, self.conf = labels, conf

    def detect(self):
        import numpy as np
        md = self.cam.capture_metadata()
        out = self.imx.get_outputs(md, add_batch=True)
        if out is None:
            return []
        boxes, scores, classes = out[0][0], out[1][0], out[2][0]
        if self.intr is not None and getattr(self.intr, "bbox_normalization", False):
            _, in_h = self.imx.get_input_size()
            boxes = boxes / in_h
        if self.intr is not None and getattr(self.intr, "bbox_order", "yx") == "xy":
            boxes = boxes[:, [1, 0, 3, 2]]
        W, H = self.size
        dets = []
        for b, s, c in zip(boxes, scores, classes):
            if s < self.conf:
                continue
            x, y, w, h = self.imx.convert_inference_coords(np.asarray(b), md, self.cam)
            dets.append((self.labels[int(c)], float(s), (x / W, y / H, (x + w) / W, (y + h) / H)))
        return dets


class OnnxDetector:
    """USB 攝影機 + YOLO11 ONNX（輸出 [1, 4+nc, N]）。"""

    def __init__(self, model_path, labels, conf, cam_index=0, imgsz=640):
        import cv2
        import onnxruntime as ort
        self.cv2, self.labels, self.conf, self.imgsz = cv2, labels, conf, imgsz
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.cap = cv2.VideoCapture(cam_index)

    def detect(self):
        import numpy as np
        cv2 = self.cv2
        ok, frame = self.cap.read()
        if not ok:
            return []
        H, W = frame.shape[:2]
        img = cv2.resize(frame, (self.imgsz, self.imgsz))[:, :, ::-1].transpose(2, 0, 1)
        x = np.ascontiguousarray(img, dtype=np.float32)[None] / 255.0
        pred = self.sess.run(None, {self.inp: x})[0][0].T          # [N, 4+nc]
        cls_scores = pred[:, 4:]
        cls = cls_scores.argmax(1)
        sc = cls_scores.max(1)
        keep = sc >= self.conf
        pred, cls, sc = pred[keep], cls[keep], sc[keep]
        cx, cy, w, h = (pred[:, i] / self.imgsz for i in range(4))
        rects = [[float(a - c / 2), float(b - d / 2), float(c), float(d)] for a, b, c, d in zip(cx, cy, w, h)]
        idx = cv2.dnn.NMSBoxes(rects, sc.tolist(), self.conf, 0.45)
        out = []
        for i in (idx.flatten() if len(idx) else []):
            x0, y0, bw, bh = rects[i]
            out.append((self.labels[int(cls[i])], float(sc[i]), (x0, y0, x0 + bw, y0 + bh)))
        return out


# ----------------------------------------------------------------------------- actuators
class Actuators:
    def __init__(self, cfg: Cfg, dry: bool):
        self.dry = dry
        if not dry:
            from gpiozero import LED, AngularServo
            self.servo = AngularServo(cfg.servo_pin, min_angle=0, max_angle=90,
                                      min_pulse_width=0.0005, max_pulse_width=0.0025)
            self.g, self.r = LED(cfg.led_green), LED(cfg.led_red)
        self.is_open = False
        self.close()

    def open(self):
        self.is_open = True
        log.info("GATE OPEN")
        if not self.dry:
            self.servo.angle = 90
            self.g.on()
            self.r.off()

    def close(self):
        self.is_open = False
        log.info("GATE CLOSE")
        if not self.dry:
            self.servo.angle = 0
            self.g.off()

    def reject(self):
        log.info("REJECT (red)")
        if not self.dry:
            self.r.blink(on_time=0.2, off_time=0.2, n=8, background=True)

    def shutdown(self):
        self.close()
        if not self.dry:
            self.servo.detach()


# ----------------------------------------------------------------------------- uploader
class Uploader(threading.Thread):
    """背景上傳，失敗寫入 spool 檔，網路恢復後補送（Challenge 4）。"""

    def __init__(self, cfg: Cfg, offline: bool):
        super().__init__(daemon=True)
        self.cfg, self.offline, self.q = cfg, offline, queue.Queue()
        self.sent: list[dict] = []

    def emit(self, etype, conf, detail):
        ev = {"bin_id": self.cfg.bin_id, "ts": time.time(), "type": etype, "confidence": round(conf, 3),
              "detail": detail}
        log.info("EVENT %s", ev)
        self.q.put(ev)

    def _post(self, ev) -> bool:
        if self.offline:
            self.sent.append(ev)
            return True
        import httpx
        try:
            r = httpx.post(f"{self.cfg.api}/api/events", json=ev, timeout=5,
                           headers={"X-Device-Token": self.cfg.token})
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    def run(self):
        spool = Path(self.cfg.spool)
        while True:
            ev = self.q.get()
            if not self._post(ev):
                spool.parent.mkdir(parents=True, exist_ok=True)
                with spool.open("a") as f:
                    f.write(json.dumps(ev) + "\n")
                continue
            if spool.exists():  # 網路通了 → 補送
                pending = [json.loads(line) for line in spool.read_text().splitlines() if line]
                left = [p for p in pending if not self._post(p)]
                spool.write_text("".join(json.dumps(p) + "\n" for p in left))


# ----------------------------------------------------------------------------- main logic
class Gate:
    def __init__(self, cfg: Cfg, act: Actuators, up: Uploader):
        self.cfg, self.act, self.up = cfg, act, up
        self.small_streak = self.reject_streak = self.ground_streak = 0
        self.open_until = 0.0
        self.last_reject = self.last_ground = -1e9
        self.next_ground = 0.0

    def step(self, dets, now):
        c = self.cfg
        slot = [d for d in dets if d[1] >= c.conf and inside(d[2], c.slot_roi) > 0.5]

        # 1) 拒收判斷：垃圾袋類別，或任何物體大到超過投入口面積比
        big = [d for d in slot if d[0] in c.bag or area_ratio(d[2], c.slot_roi) > c.max_item_area]
        small = [d for d in slot if d[0] in c.small and d not in big]
        hand = any(d[0] in c.no_close for d in slot)

        self.reject_streak = self.reject_streak + 1 if big else 0
        self.small_streak = self.small_streak + 1 if small and not big else 0

        if self.reject_streak >= c.confirm_frames:
            if self.act.is_open and not hand:
                self.act.close()
            if now - self.last_reject > c.reject_cooldown_s:
                d = max(big, key=lambda x: x[1])
                self.act.reject()
                self.up.emit("bag_rejected", d[1], {"cls": d[0], "area_ratio": round(area_ratio(d[2], c.slot_roi), 2)})
                self.last_reject = now
        elif self.small_streak >= c.confirm_frames:
            if not self.act.is_open:
                self.act.open()
            self.open_until = now + c.open_s

        if self.act.is_open and now > self.open_until and not hand:
            self.act.close()

        # 2) 地面監看：低頻率計算覆蓋率
        if now >= self.next_ground:
            self.next_ground = now + c.ground_period_s
            gboxes = [d[2] for d in dets if d[0] in c.ground_waste and d[1] >= c.conf
                      and inside(d[2], c.ground_roi) > 0.5]
            cov = coverage(gboxes, c.ground_roi) if gboxes else 0.0
            self.ground_streak = self.ground_streak + 1 if cov >= c.ground_cov_thr else 0
            if self.ground_streak >= c.ground_confirm and now - self.last_ground > c.ground_cooldown_s:
                conf = max(d[1] for d in dets if d[2] in gboxes)
                self.up.emit("ground_waste", conf, {"coverage": round(cov, 3), "n": len(gboxes)})
                self.last_ground = now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["imx500", "onnx", "mock"], default="imx500")
    ap.add_argument("--model", default="/opt/smartbin/models/network.rpk")
    ap.add_argument("--scenario", help="mock 情境檔")
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--dry", action="store_true", help="不驅動 GPIO")
    ap.add_argument("--offline", action="store_true", help="不上傳，只印事件")
    ap.add_argument("--sim-dt", type=float, default=0, help="mock 時每幀推進的模擬秒數")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = Cfg()
    if a.backend == "mock":
        det = MockDetector(a.scenario)
    elif a.backend == "onnx":
        det = OnnxDetector(a.model, cfg.labels, cfg.conf)
    else:
        det = IMX500Detector(a.model, cfg.labels, cfg.conf)

    act = Actuators(cfg, dry=a.dry or a.backend == "mock")
    up = Uploader(cfg, offline=a.offline)
    up.start()
    gate = Gate(cfg, act, up)
    t = time.time()
    try:
        while True:
            dets = det.detect()
            if dets is None:
                break
            t = t + a.sim_dt if a.sim_dt else time.time()
            gate.step(dets, t)
            if not a.sim_dt:
                time.sleep(1 / a.fps)
    except KeyboardInterrupt:
        pass
    finally:
        act.shutdown()
        time.sleep(0.5)
    return up


if __name__ == "__main__":
    main()
