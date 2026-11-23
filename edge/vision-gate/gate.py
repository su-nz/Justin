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
    # SG90：0.5–2.5 ms 對應 0–180°。開／關角度以擋板實際到位為準，可用 --open-angle / --close-angle 調
    servo_min_pulse: float = 0.0005
    servo_max_pulse: float = 0.0025
    servo_open_angle: float = 90.0
    servo_close_angle: float = 0.0
    servo_settle_s: float = 0.6     # SG90 轉 90° 約 0.3 s；到位後保留訊號這麼久再放掉
    # 到位後停 PWM（detach）：軟體 PWM（沒有 pigpio）時伺服會一直微抖。機構要伺服一直出力才撐得住時設 True
    servo_hold: bool = False
    led_green: int = 23
    led_red: int = 24
    buzzer_pin: int = 25
    state_period_s: float = 3.0     # 多久向後端查一次桶子狀態（#55：原本 60 秒，桶滿／清空要等太久）
    # 感測被擋（fault）時也鎖閘門：不知道桶滿不滿，先不收（#55；#30 的可切換實作）
    lock_when_blocked: bool = os.environ.get("GATE_LOCK_BLOCKED", "1") != "0"
    policy_file: str = os.environ.get("GATE_POLICY_FILE", "/tmp/smartbin-gate-policy")
    spool: str = os.environ.get("GATE_SPOOL", "/var/lib/smartbin/spool.jsonl")
    # 擋板最新動作，給 node_pi.py 顯示在 LCD（#36）；空字串 = 不寫
    status_file: str = os.environ.get("GATE_STATUS_FILE", "/tmp/smartbin-gate.json")
    # 遠端控制（#41）
    command_period_s: float = 2.0
    status_period_s: float = 2.0    # 元件狀態上報間隔（#53）
    manual_open_s: float = 10.0
    mode_file: str = os.environ.get("GATE_MODE_FILE", "/tmp/smartbin-gate-mode")  # run-gate.sh 依此選模式


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


# ----------------------------------------------------------------------------- COCO（#11 L1：免訓練模型）
# Ultralytics 預訓練 YOLO11n 的 80 類，順序即模型輸出的類別編號
COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)
# COCO 類別 → 閘門類別。沒列到的一律 "other"：不會開閘，但體積過大一樣走「太大就拒收」
COCO_TO_GATE = {
    "cup": "cup", "wine glass": "cup", "bottle": "bottle",
    "person": "hand",                                                         # 有人在投入口就不關閘
    "handbag": "garbage_bag", "backpack": "garbage_bag", "suitcase": "garbage_bag",  # 袋狀物直接拒收
}


def coco_to_gate(name: str) -> str:
    return COCO_TO_GATE.get(name, "other")


# ----------------------------------------------------------------------------- detectors
# 空清單代表一幀已處理但沒有辨識結果，None 代表劇本播放完畢。
FRAME_UNAVAILABLE = object()


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
            return FRAME_UNAVAILABLE
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


def camera_source(value: str) -> int | str:
    """--camera：純數字當裝置編號（2 → /dev/video2），其他照原樣交給 OpenCV（裝置路徑、影片檔）。"""
    value = value.strip()
    return int(value) if value.isdigit() else value


class OnnxDetector:
    """USB 攝影機 + YOLO11 ONNX（輸出 [1, 4+nc, N]）。"""

    def __init__(self, model_path, labels, conf, cam_index=0, imgsz=None, remap=None):
        import cv2
        import onnxruntime as ort
        self.cv2, self.labels, self.conf, self.remap = cv2, labels, conf, remap
        # 不限制的話 onnxruntime 會吃光 Pi 4 全部核心，node_pi.py 量超音波／HX711 的時序就會錯過（#43）
        threads = int(os.environ.get("GATE_ORT_THREADS", "2"))
        so = ort.SessionOptions()
        so.intra_op_num_threads, so.inter_op_num_threads = threads, 1
        cv2.setNumThreads(1)
        self.sess = ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.inp = inp.name
        # 沒指定就用模型本身的輸入尺寸（例如 imgsz=320 匯出的模型），動態尺寸才退回 640
        self.imgsz = imgsz or (inp.shape[2] if isinstance(inp.shape[2], int) else 640)
        self.cap = cv2.VideoCapture(cam_index)
        if not self.cap.isOpened():
            raise SystemExit(f"打不開攝影機 {cam_index!r}：用 v4l2-ctl --list-devices 確認編號，再用 --camera 指定")

    def detect(self):
        ok, frame = self.cap.read()
        if not ok:
            return FRAME_UNAVAILABLE
        import numpy as np
        cv2 = self.cv2
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
            name = self.labels[int(cls[i])]
            out.append((self.remap(name) if self.remap else name, float(sc[i]), (x0, y0, x0 + bw, y0 + bh)))
        return out


# ----------------------------------------------------------------------------- actuators
def write_gate_status(path: str, state: str, now: float | None = None) -> None:
    """原子寫入 {"state": open|close|reject, "ts": ...}；寫不進去只記 log，不影響閘門。"""
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        Path(tmp).write_text(json.dumps({"state": state, "ts": now or time.time()}))
        os.replace(tmp, path)
    except OSError as e:
        log.debug("寫入閘門狀態檔失敗：%s", e)


def servo_pin_factory():
    """伺服用 pigpio 的硬體計時 PWM，比 gpiozero 預設的軟體 PWM 穩，不會抖。

    要先 sudo systemctl enable --now pigpiod；沒開就退回預設，並提醒一次。
    """
    try:
        from gpiozero.pins.pigpio import PiGPIOFactory
        return PiGPIOFactory()
    except Exception as e:  # 沒裝 pigpio、pigpiod 沒啟動
        log.warning("伺服改用軟體 PWM，可能會抖（%s）；請執行 sudo systemctl enable --now pigpiod", e)
        return None


class Actuators:
    def __init__(self, cfg: Cfg, dry: bool):
        self.dry = dry
        self.cfg = cfg
        if not dry:
            from gpiozero import LED, AngularServo, Buzzer
            self.servo = AngularServo(cfg.servo_pin, min_angle=0, max_angle=180,
                                      min_pulse_width=cfg.servo_min_pulse,
                                      max_pulse_width=cfg.servo_max_pulse,
                                      # 第一個 PWM 就是校正後的關門角度，不要先送預設的 0°
                                      initial_angle=cfg.servo_close_angle,
                                      pin_factory=servo_pin_factory())
            self.g, self.r = LED(cfg.led_green), LED(cfg.led_red)
            self.buzzer = Buzzer(cfg.buzzer_pin)
        self.is_open = False
        self.last_reject_ts: float | None = None
        self._detach_timer: threading.Timer | None = None
        self.close()

    def _move(self, angle: float, settle: bool = True) -> None:
        """轉到 angle；不是 servo_hold 時，到位後停 PWM，下次設定角度 gpiozero 會自動恢復輸出。"""
        if self._detach_timer is not None:
            self._detach_timer.cancel()  # 上一個動作的 detach 不能打斷這次轉動
            self._detach_timer = None
        self.servo.angle = angle
        if settle and not self.cfg.servo_hold:
            self._detach_timer = threading.Timer(self.cfg.servo_settle_s, self.servo.detach)
            self._detach_timer.daemon = True
            self._detach_timer.start()

    def open(self):
        self.is_open = True
        log.info("GATE OPEN")
        if not self.dry:
            self._move(self.cfg.servo_open_angle)
            self.g.on()
            self.r.off()
            write_gate_status(self.cfg.status_file, "open")

    def close(self, settle: bool = True):
        self.is_open = False
        log.info("GATE CLOSE")
        if not self.dry:
            self._move(self.cfg.servo_close_angle, settle)
            self.g.off()
            write_gate_status(self.cfg.status_file, "close")

    def reject(self):
        log.info("REJECT (red)")
        self.last_reject_ts = time.time()
        if not self.dry:
            self.r.blink(on_time=0.2, off_time=0.2, n=8, background=True)
            self.buzzer.beep(on_time=0.2, off_time=0.2, n=3, background=True)
            write_gate_status(self.cfg.status_file, "reject")

    def shutdown(self):
        self.close(settle=False)  # 這裡自己等、自己 detach，不排計時器
        if not self.dry:
            time.sleep(self.cfg.servo_settle_s)  # 保留關門訊號，讓擋板關到位再放掉
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
            if 400 <= r.status_code < 500:  # 重送也不會成功（多半是 token 或 bin_id 設錯），至少要看得到
                log.warning("event dropped: HTTP %s %s", r.status_code, r.text)
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


class BinStateWatcher(threading.Thread):
    """定期向後端查桶子狀態，桶滿時閘門不開。查不到就沿用上次結果（預設未滿），斷網不會讓桶停擺。"""

    def __init__(self, cfg: Cfg, gate: "Gate"):
        super().__init__(daemon=True)
        self.cfg, self.gate = cfg, gate

    def poll(self):
        import httpx
        try:
            bins = httpx.get(f"{self.cfg.api}/api/bins", timeout=5).json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("bin state unavailable: %s", e)
            return
        for b in bins:
            if b["id"] == self.cfg.bin_id:
                self.gate.bin_full = b["state"] == "full" or bool(b.get("full_for_gate", False))
                self.gate.bin_blocked = b["state"] == "fault"

    def run(self):
        while True:
            time.sleep(self.cfg.state_period_s)
            self.poll()


class CommandPoller(threading.Thread):
    """每 2 秒向後端拿調度台送來的指令（#41），放進 gate.commands 由主迴圈執行；結果回報後端。"""

    def __init__(self, cfg: Cfg, gate: "Gate"):
        super().__init__(daemon=True)
        self.cfg, self.gate = cfg, gate

    def poll(self):
        if self.gate.stopping:  # 快結束了就不要再拿指令，免得拿了卻沒執行
            return
        import httpx
        try:
            r = httpx.get(f"{self.cfg.api}/api/device/commands", timeout=5,
                          params={"bin_id": self.cfg.bin_id, "target": "gate"},
                          headers={"X-Device-Token": self.cfg.token})
            if r.status_code != 200:
                log.debug("commands unavailable: HTTP %s", r.status_code)
                return
            for c in r.json():
                self.gate.commands.put(c)
        except (httpx.HTTPError, ValueError) as e:
            log.debug("commands unavailable: %s", e)

    def report(self, cid: int, ok: bool, result):
        import httpx
        try:
            httpx.post(f"{self.cfg.api}/api/device/commands/{cid}/result", timeout=5,
                       params={"bin_id": self.cfg.bin_id}, json={"ok": ok, "result": result},
                       headers={"X-Device-Token": self.cfg.token})
        except httpx.HTTPError as e:
            log.warning("command result not sent: %s", e)

    def run(self):
        while True:
            self.poll()
            time.sleep(self.cfg.command_period_s)


class StatusReporter(threading.Thread):
    """每 2 秒把閘門元件狀態（模式、相機、擋板、最後辨識結果）報給後端（#53）。只有文字與座標，不傳影像。"""

    def __init__(self, cfg: Cfg, gate: "Gate"):
        super().__init__(daemon=True)
        self.cfg, self.gate = cfg, gate

    def post(self) -> bool:
        if self.gate.stopping:  # 程式結束時 shutdown 會關門，那是暫態，不要報給調度台
            return False
        import httpx
        try:
            r = httpx.post(f"{self.cfg.api}/api/device/status", timeout=5,
                           json={"bin_id": self.cfg.bin_id, "source": "gate", "components": self.gate.snapshot()},
                           headers={"X-Device-Token": self.cfg.token})
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.debug("status not sent: %s", e)
            return False

    def run(self):
        while True:
            self.post()
            time.sleep(self.cfg.status_period_s)


# ----------------------------------------------------------------------------- main logic
class Gate:
    def __init__(self, cfg: Cfg, act: Actuators, up: Uploader):
        self.cfg, self.act, self.up = cfg, act, up
        self.small_streak = self.reject_streak = self.ground_streak = 0
        self.open_until = 0.0
        self.last_reject = self.last_ground = -1e9
        self.next_ground = 0.0
        self.bin_full = False
        self.bin_blocked = False
        self.stopping = False
        self.policy = self._load_policy()   # auto | force_open | force_lock（#55）
        self.reason = "等待投入"             # 目前判斷原因，給調度台看
        if self.policy == "force_open":      # 換模式重開後立刻套用保留的策略
            self.act.open()
            self.open_until = float("inf")
            self.reason = "強制開啟"
        elif self.policy == "force_lock":
            self.reason = "強制鎖定"
        self.commands: queue.Queue = queue.Queue()
        self.reporter = None          # CommandPoller；離線時沒有
        self.restart_mode: str | None = None  # 收到 gate_mode → 主迴圈結束，run-gate.sh 換模式重開
        # 元件狀態（#53）：主迴圈更新，StatusReporter 讀取
        self.mode = "mock"
        self.camera = {"state": "starting", "fps": None, "valid": 0, "attempts": 0, "detect_ms": None}
        self.last_dets: list = []
        self.last_dets_ts: float | None = None
        self.last_action: tuple[str, float] | None = None

    POLICIES = ("auto", "force_open", "force_lock")
    LABEL = {"cup": "杯子", "bottle": "瓶子", "can": "鋁罐", "paper": "紙", "lunchbox": "便當盒",
             "garbage_bag": "垃圾袋", "hand": "手／人", "litter": "垃圾", "other": "其他物品"}

    def _load_policy(self) -> str:
        try:
            p = Path(self.cfg.policy_file).read_text().strip()
        except OSError:
            return "auto"
        return p if p in self.POLICIES else "auto"

    def lock_reason(self) -> str | None:
        """目前不能收垃圾的原因；None = 可以收。"""
        if self.policy == "force_open":  # 強制開啟優先於所有鎖定
            return None
        if self.policy == "force_lock":
            return "強制鎖定"
        if self.bin_full:
            return "桶滿鎖定"
        if self.bin_blocked and self.cfg.lock_when_blocked:
            return "感測被擋鎖定"
        return None

    def note_frame(self, dets, now: float) -> None:
        """記下最近一次有辨識到東西的結果（最多 10 個），給調度台畫示意框。"""
        if dets and dets is not FRAME_UNAVAILABLE:
            self.last_dets = [{"cls": d[0], "conf": round(float(d[1]), 2), "box": [round(float(v), 3) for v in d[2]]}
                              for d in sorted(dets, key=lambda d: -d[1])[:10]]
            self.last_dets_ts = time.time()

    def snapshot(self) -> dict:
        c = self.cfg
        return {
            "mode": self.mode,
            "camera": dict(self.camera),
            "gate": {"open": bool(self.act.is_open), "bin_full_lock": bool(self.bin_full),
                     "bin_blocked": bool(self.bin_blocked), "lock_when_blocked": self.cfg.lock_when_blocked,
                     "policy": self.policy, "lock": self.lock_reason(), "reason": self.reason,
                     "last_reject_ts": getattr(self.act, "last_reject_ts", None)},
            "detections": {"items": self.last_dets, "ts": self.last_dets_ts,
                           "slot_roi": list(c.slot_roi), "ground_roi": list(c.ground_roi)},
            "last_action": None if self.last_action is None else {"what": self.last_action[0], "ts": self.last_action[1]},
        }

    def drain_commands(self, now: float) -> None:
        """程式結束前：已拿到但還沒執行的指令不能默默消失。策略只寫檔，照做；其他回報失敗請使用者再按一次。"""
        while True:
            try:
                c = self.commands.get_nowait()
            except queue.Empty:
                return
            if c["cmd"] in ("gate_policy", "gate_mode"):
                ok, result = self.handle_command(c["cmd"], c.get("args") or {}, now)
            else:
                ok, result = False, "閘門正在重新啟動，請再按一次"
            if self.reporter is not None:
                self.reporter.report(c["id"], ok, result)

    def run_commands(self, now: float) -> None:
        """主迴圈每一幀呼叫：執行佇列裡的遠端指令（硬體只在主執行緒動）。"""
        while True:
            try:
                c = self.commands.get_nowait()
            except queue.Empty:
                return
            ok, result = self.handle_command(c["cmd"], c.get("args") or {}, now)
            log.info("REMOTE %s %s → %s", c["cmd"], c.get("args") or "", result)
            if self.reporter is not None:
                self.reporter.report(c["id"], ok, result)

    def handle_command(self, cmd: str, args: dict, now: float) -> tuple[bool, str]:
        if cmd == "gate_open":
            if not self.act.is_open:
                self.act.open()
            self.open_until = now + self.cfg.manual_open_s
            return True, f"已開門 {self.cfg.manual_open_s:g} 秒"
        if cmd == "gate_close":
            self.open_until = 0.0
            if self.act.is_open:
                self.act.close()
            return True, "已關門"
        if cmd == "gate_reject":
            self.act.reject()
            return True, "已觸發紅燈與蜂鳴器"
        if cmd == "gate_policy":
            policy = args.get("policy")
            if policy not in self.POLICIES:
                return False, f"不支援的策略 {policy!r}"
            self.policy = policy
            try:
                Path(self.cfg.policy_file).write_text(policy)
            except OSError as e:
                log.warning("閘門策略寫不進檔案（重開後會回到 auto）：%s", e)
            if policy == "force_open":
                if not self.act.is_open:
                    self.act.open()
            elif policy == "force_lock":
                self.open_until = 0.0
                if self.act.is_open:
                    self.act.close()
            else:
                self.open_until = now  # 回到自動：交給相機判斷，沒看到東西就關
            return True, {"auto": "自動（相機判斷）", "force_open": "強制開啟", "force_lock": "強制鎖定"}[policy]
        if cmd == "gate_mode":
            mode = args.get("mode")
            if mode not in ("mock", "camera"):
                return False, f"不支援的模式 {mode!r}"
            try:
                Path(self.cfg.mode_file).write_text(mode)
            except OSError as e:
                return False, f"寫不進模式檔：{e}"
            self.restart_mode = mode
            return True, f"切換到{'相機即時辨識' if mode == 'camera' else '劇本'}模式，閘門重新啟動中"
        return False, f"閘門不支援指令 {cmd}"

    def step(self, dets, now):
        c = self.cfg
        # 物體大半在投入口內，或投入口大半被物體蓋住（貼近鏡頭的大袋框會比 ROI 大很多）
        slot = [d for d in dets if d[1] >= c.conf
                and (inside(d[2], c.slot_roi) > 0.5 or inside(c.slot_roi, d[2]) > 0.5)]

        # 1) 拒收判斷：垃圾袋類別，或任何物體大到超過投入口面積比（手不算，拿著杯子的手本來就大）
        big = [d for d in slot if d[0] in c.bag
               or (d[0] not in c.no_close and area_ratio(d[2], c.slot_roi) > c.max_item_area)]
        small = [d for d in slot if d[0] in c.small and d not in big]
        hand = any(d[0] in c.no_close for d in slot)

        self.reject_streak = self.reject_streak + 1 if big else 0
        self.small_streak = self.small_streak + 1 if small and not big else 0
        lock = self.lock_reason()

        if self.policy == "force_open":  # 強制開啟：不管看到什麼都開著（地面監看照常）
            if not self.act.is_open:
                self.act.open()
            self.open_until = now + c.open_s
            self.reason = "強制開啟"
        elif self.reject_streak >= c.confirm_frames:
            if self.act.is_open and not hand:
                self.act.close()
            if self.reject_streak == c.confirm_frames:  # 每次投入都要提示；冷卻只限制上傳
                self.act.reject()
            if now - self.last_reject > c.reject_cooldown_s:
                d = max(big, key=lambda x: x[1])
                self.up.emit("bag_rejected", d[1], {"cls": d[0], "area_ratio": round(area_ratio(d[2], c.slot_roi), 2)})
                self.last_reject = now
        elif self.small_streak >= c.confirm_frames:
            if lock:  # 桶滿／被擋／強制鎖定：小垃圾也不開，市民由地圖 / LINE 導向下一個有空位的桶
                if self.small_streak == c.confirm_frames:
                    self.act.reject()
            else:
                if not self.act.is_open:
                    self.act.open()
                self.open_until = now + c.open_s

        if self.policy != "force_open" and self.act.is_open and now > self.open_until and not hand:
            self.act.close()
        if self.policy != "force_open":
            self.reason = self._explain(dets, slot, big, small, hand, lock, now)

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


    def _explain(self, dets, slot, big, small, hand, lock, now) -> str:
        """一句話說明這一幀閘門為什麼是現在這樣（#55）。"""
        c, name = self.cfg, lambda d: self.LABEL.get(d[0], d[0])
        if big and self.reject_streak >= c.confirm_frames:
            d = max(big, key=lambda x: x[1])
            return f"拒收：{'垃圾袋' if d[0] in c.bag else name(d) + '太大'}"
        if lock:
            return f"{lock}：看到{name(small[0])}也不收" if small else lock
        if self.act.is_open:
            if hand and now > self.open_until:
                return "投入口有手，暫不關門"
            if small:
                return f"開門：看到{name(small[0])}"
            return f"開啟中，{max(0, self.open_until - now):.0f} 秒後關門"
        if small or big:
            return "確認中…"
        seen = [d for d in dets if d[1] >= c.conf and d not in slot]
        others = [d for d in slot if d[0] not in c.small and d[0] not in c.no_close]
        if others:
            return f"投入口有{name(others[0])}，不是可投物品"
        if seen:
            return f"看到{name(seen[0])}，但不在投入口"
        if hand:
            return "投入口有手，沒有可投物品"
        return "等待投入"


def actuators_dry(a) -> bool:
    """--dry 一律不碰 GPIO；mock 劇本預設也不碰，加 --drive 才驅動實體元件。"""
    return a.dry or (a.backend == "mock" and not a.drive)


def lower_priority() -> None:
    """閘門讓感測程式先跑：視覺推論慢一點沒關係，量測時序錯過就沒有資料（#43）。"""
    inc = int(os.environ.get("GATE_NICE", "10"))
    if inc > 0:
        try:
            os.nice(inc)
        except (OSError, AttributeError) as e:
            log.debug("無法調整優先權：%s", e)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["imx500", "onnx", "mock"], default="imx500")
    ap.add_argument("--model", default="/opt/smartbin/models/network.rpk")
    ap.add_argument("--scenario", help="mock 情境檔")
    ap.add_argument("--camera", type=camera_source, default=camera_source(os.environ.get("GATE_CAMERA", "0")),
                    help="onnx 用的攝影機：編號（0 → /dev/video0）或裝置路徑；預設讀環境變數 GATE_CAMERA，沒有就是 0")
    ap.add_argument("--fps", type=float, default=5)
    ap.add_argument("--dry", action="store_true", help="不驅動 GPIO")
    ap.add_argument("--drive", action="store_true",
                    help="mock 劇本也驅動實體伺服、LED、蜂鳴器（#6 劇本模式；建議搭配 --fps 1）")
    ap.add_argument("--open-angle", type=float, help="擋板打開時的伺服角度（0–180，預設 90）")
    ap.add_argument("--close-angle", type=float, help="擋板關閉時的伺服角度（0–180，預設 0）")
    ap.add_argument("--servo-hold", action="store_true",
                    help="到位後不停 PWM，伺服一直出力（擋板撐不住時用；軟體 PWM 會微抖）")
    ap.add_argument("--offline", action="store_true", help="不上傳，只印事件")
    ap.add_argument("--sim-dt", type=float, default=0, help="mock 時每幀推進的模擬秒數")
    ap.add_argument("--labels", choices=["smartbin", "coco"], default="smartbin",
                    help="onnx 模型的類別：smartbin＝自訓 8 類；coco＝預訓練 80 類，對應到閘門類別（#11）")
    ap.add_argument("--imgsz", type=int, help="onnx 輸入尺寸，預設讀模型本身的設定")
    a = ap.parse_args(argv)
    for name in ("open_angle", "close_angle"):
        v = getattr(a, name)
        if v is not None and not 0 <= v <= 180:
            ap.error(f"--{name.replace('_', '-')} 要在 0–180 之間")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    cfg = Cfg()
    if a.open_angle is not None:
        cfg.servo_open_angle = a.open_angle
    if a.close_angle is not None:
        cfg.servo_close_angle = a.close_angle
    cfg.servo_hold = a.servo_hold
    if a.backend == "mock":
        det = MockDetector(a.scenario)
    elif a.backend == "onnx":
        if a.labels == "coco":
            det = OnnxDetector(a.model, COCO_NAMES, cfg.conf, cam_index=a.camera, imgsz=a.imgsz, remap=coco_to_gate)
        else:
            det = OnnxDetector(a.model, cfg.labels, cfg.conf, cam_index=a.camera, imgsz=a.imgsz)
    else:
        det = IMX500Detector(a.model, cfg.labels, cfg.conf)

    lower_priority()
    act = Actuators(cfg, dry=actuators_dry(a))
    up = Uploader(cfg, offline=a.offline)
    up.start()
    gate = Gate(cfg, act, up)
    if not a.offline:
        watcher = BinStateWatcher(cfg, gate)
        watcher.poll()
        watcher.start()
        if cfg.token:
            gate.reporter = CommandPoller(cfg, gate)
            gate.reporter.start()
            StatusReporter(cfg, gate).start()
    gate.mode = a.backend
    if a.backend == "mock":
        gate.camera = {"state": "scenario", "fps": a.fps, "valid": None, "attempts": None, "detect_ms": None}
    t = time.time()
    attempts, processed, det_s, w0 = 0, 0, 0.0, time.perf_counter()
    try:
        while True:
            t0 = time.perf_counter()
            dets = det.detect()
            if dets is None:
                break
            det_s += time.perf_counter() - t0
            t = t + a.sim_dt if a.sim_dt else time.time()
            gate.run_commands(t)
            if gate.restart_mode:
                log.info("模式切換為 %s，結束讓 run-gate.sh 重新啟動", gate.restart_mode)
                break
            was_open = act.is_open
            if dets is FRAME_UNAVAILABLE:
                gate.step([], t)
            else:
                gate.note_frame(dets, t)
                gate.step(dets, t)
                processed += 1
            if act.is_open != was_open:
                gate.last_action = ("open" if act.is_open else "close", time.time())
            if not a.sim_dt:
                time.sleep(1 / a.fps)
            attempts += 1
            if attempts == 20 and a.backend != "mock":
                now = time.perf_counter()
                log.info("實際 %.1f FPS（有效影格 %d/%d，含等待）；detect 平均 %.0f ms/次",
                         processed / (now - w0), processed, attempts, det_s / attempts * 1000)
                gate.camera = {"state": "ok" if processed else "no_frames",
                               "fps": round(processed / (now - w0), 1), "valid": processed,
                               "attempts": attempts, "detect_ms": round(det_s / attempts * 1000)}
                attempts, processed, det_s, w0 = 0, 0, 0.0, now
    except KeyboardInterrupt:
        pass
    finally:
        gate.stopping = True
        gate.drain_commands(time.time())
        act.shutdown()
        time.sleep(0.5)
    return up


if __name__ == "__main__":
    main()
