"""Smart Bin 雲端後端（FastAPI）。

資料進來兩條路：
- MQTT（Wi-Fi 節點）：smartbin/<bin_id>/telemetry、smartbin/<bin_id>/event，見 mqtt_ingest.py
- HTTP（NB-IoT 節點、模擬器）：POST /api/telemetry、POST /api/events，用每桶各自的 X-Device-Token

對外：
- 市民地圖 PWA：GET /api/bins、GET /api/nearest
- 清運端：POST /api/dispatch/plan、GET /api/dispatch/latest、GET /api/events
- LINE 官方帳號：POST /line/webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, line_bot, logic, routing

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
DEPOT = (float(os.environ.get("DEPOT_LAT", "25.0236")), float(os.environ.get("DEPOT_LNG", "121.5440")))
EVENT_TYPES = {"bag_rejected", "ground_waste", "overflow", "illegal_dumping", "sensor_blocked",
               "tamper", "fire_smoke"}
OPEN_EVENT_TYPES = {"overflow", "illegal_dumping", "ground_waste_unknown", "fire_smoke"}

app = FastAPI(title="Smart Bin API", version="0.1.0")


@app.on_event("startup")
def _startup():
    db.init_db()
    if os.environ.get("MQTT_HOST"):
        from .mqtt_ingest import start
        start(ingest_telemetry, ingest_event)


# ---------------------------------------------------------------- auth
def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def require_admin(authorization: str = Header("")):
    if not hmac.compare_digest(authorization, f"Bearer {ADMIN_TOKEN}"):
        raise HTTPException(401, "admin token required")


def device_auth(bin_id: str, token: str) -> None:
    with db.conn() as c:
        row = c.execute("SELECT token_hash FROM bins WHERE id=?", (bin_id,)).fetchone()
    if not row or not hmac.compare_digest(row["token_hash"], _hash(token)):
        raise HTTPException(401, "bad device token")


# ---------------------------------------------------------------- models
class BinIn(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    name: str
    lat: float
    lng: float
    capacity_l: float = 60
    depth_mm: int = 700
    has_gate: bool = False


class TelemetryIn(BaseModel):
    bin_id: str
    ts: float | None = None
    fill_pct: float | None = None
    dist_mm: int | None = None
    weight_g: float | None = None
    battery_mv: int | None = None
    rssi: int | None = None
    blocked: bool = False


class EventIn(BaseModel):
    bin_id: str
    ts: float | None = None
    type: str
    confidence: float | None = None
    detail: dict | None = None


# 遠端控制（#41）：指令 → 由哪支程式執行
COMMANDS = {"gate_open": "gate", "gate_close": "gate", "gate_reject": "gate", "gate_mode": "gate", "gate_policy": "gate",
            "lcd_text": "node", "measure_now": "node", "tare": "node"}
GATE_MODES = {"mock", "camera"}
GATE_POLICIES = {"auto", "force_open", "force_lock"}
COMMAND_TTL_S = 60  # 超過這麼久沒被裝置取走就不送（避免裝置離線很久後才突然動作）


class CommandIn(BaseModel):
    cmd: str
    args: dict = Field(default_factory=dict)


class CommandResultIn(BaseModel):
    ok: bool
    result: dict | str | None = None


STATUS_MAX_BYTES = 8192  # 元件狀態只有文字與座標，不收影像（#53）


class DeviceStatusIn(BaseModel):
    bin_id: str
    source: str = Field(pattern="^(node|gate)$")
    components: dict


class PlanIn(BaseModel):
    vehicles: int = Field(1, ge=1, le=10)
    vehicle_capacity_l: int = Field(2000, ge=100)
    horizon_h: float = Field(4.0, ge=0, le=24)


# ---------------------------------------------------------------- ingest (shared by HTTP & MQTT)
def ingest_telemetry(t: TelemetryIn) -> None:
    with db.conn() as c:
        b = c.execute("SELECT depth_mm FROM bins WHERE id=?", (t.bin_id,)).fetchone()
        if not b:
            raise HTTPException(404, "unknown bin")
        fill = t.fill_pct
        if fill is None and t.dist_mm is not None:
            fill = (b["depth_mm"] - t.dist_mm) / max(b["depth_mm"] - 50, 1) * 100
        if fill is None:
            raise HTTPException(422, "fill_pct or dist_mm required")
        fill = min(max(fill, 0.0), 100.0)
        ts = t.ts if t.ts and t.ts <= time.time() + 60 else time.time()
        c.execute("INSERT INTO telemetry(bin_id,ts,fill_pct,dist_mm,weight_g,battery_mv,rssi,blocked)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (t.bin_id, ts, fill, t.dist_mm, t.weight_g, t.battery_mv, t.rssi, int(t.blocked)))
    if t.blocked:
        _record_event(t.bin_id, ts, "sensor_blocked", None, {"dist_mm": t.dist_mm}, dedupe_s=3600)


def ingest_event(e: EventIn) -> dict:
    if e.type not in EVENT_TYPES:
        raise HTTPException(422, f"type must be one of {sorted(EVENT_TYPES)}")
    ts = e.ts or time.time()
    etype = e.type
    if etype == "ground_waste":  # 融合液位：overflow vs illegal_dumping
        st = _bin_status(e.bin_id)
        etype = logic.fuse_ground_event(st.fill_pct if st else None)
    _record_event(e.bin_id, ts, etype, e.confidence, e.detail or {}, dedupe_s=900)
    return {"bin_id": e.bin_id, "type": etype}


def _record_event(bin_id, ts, etype, conf, detail, dedupe_s=0):
    with db.conn() as c:
        if dedupe_s:
            dup = c.execute("SELECT 1 FROM events WHERE bin_id=? AND type=? AND resolved=0 AND ts>?",
                            (bin_id, etype, ts - dedupe_s)).fetchone()
            if dup:
                return
        c.execute("INSERT INTO events(bin_id,ts,type,confidence,detail) VALUES(?,?,?,?,?)",
                  (bin_id, ts, etype, conf, json.dumps(detail, ensure_ascii=False)))


# ---------------------------------------------------------------- status
def _bin_status(bin_id: str, now: float | None = None) -> logic.BinStatus | None:
    now = now or time.time()
    cutoff = now - 24 * 3600
    with db.conn() as c:
        # Retain the latest weight for display even if all readings are offline.
        rows = c.execute(
            "SELECT ts, fill_pct, weight_g, blocked FROM telemetry WHERE bin_id=? "
            "AND ts>=min(?, (SELECT max(ts) FROM telemetry WHERE bin_id=?)) ORDER BY ts, id",
            (bin_id, cutoff, bin_id),
        ).fetchall()
    if not rows:
        return logic.status_of([], False, now=now)
    latest = rows[-1]
    samples = [(r["ts"], r["fill_pct"]) for r in rows if r["ts"] > cutoff]
    # Use the latest report as a unit: a missing weight means the scale timed
    # out, so an older heavy reading must not keep the bin classified as full.
    weight_g = latest["weight_g"]
    prev = None  # 依時間跑一遍狀態機，讓遲滯生效
    for _, f in samples[-12:-1]:
        prev = logic.classify(f, prev)
    return logic.status_of(samples, bool(latest["blocked"]), prev, now, weight_g)


def _all_bins() -> list[dict]:
    with db.conn() as c:
        bins = [dict(r) for r in c.execute(
            "SELECT id,name,lat,lng,capacity_l,depth_mm,has_gate FROM bins ORDER BY id")]
        # 最新一筆原始讀值（調度台顯示用，不進 PUBLIC_FIELDS）
        raw = {r["bin_id"]: r for r in c.execute(
            "SELECT t.bin_id, t.dist_mm, t.blocked FROM telemetry t JOIN "
            "(SELECT bin_id, max(id) AS id FROM telemetry GROUP BY bin_id) m ON t.id = m.id")}
        evs = c.execute("SELECT bin_id, type FROM events WHERE resolved=0 AND ts>?",
                        (time.time() - 24 * 3600,)).fetchall()
    open_ev: dict[str, list[str]] = {}
    for e in evs:
        if e["type"] in OPEN_EVENT_TYPES:
            open_ev.setdefault(e["bin_id"], []).append(e["type"])
    for b in bins:
        st = _bin_status(b["id"])
        b.update(state=st.state, fill_pct=st.fill_pct, rate_pct_per_h=st.rate_pct_per_h,
                 hours_to_full=st.hours_to_full, last_seen=st.last_seen,
                 weight_g=st.weight_g, full_for_gate=st.full_for_gate,
                 open_events=open_ev.get(b["id"], []), has_gate=bool(b["has_gate"]))
        r = raw.get(b["id"])
        b.update(dist_mm=r["dist_mm"] if r else None, blocked=bool(r["blocked"]) if r else None)
    return bins


HISTORY_MAX_HOURS = 168


def telemetry_history(bin_id: str, hours: float, max_points: int, now: float | None = None) -> list[dict]:
    """最近 hours 小時的 telemetry，依時間排序；超過 max_points 就等距抽樣（保留最後一筆）。"""
    now = now or time.time()
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM bins WHERE id=?", (bin_id,)).fetchone():
            raise HTTPException(404, "unknown bin")
        rows = c.execute(
            "SELECT ts, fill_pct, dist_mm, weight_g, blocked FROM telemetry "
            "WHERE bin_id=? AND ts>=? ORDER BY ts, id", (bin_id, now - hours * 3600)).fetchall()
    if len(rows) > max_points:
        step = len(rows) / max_points
        picked = [rows[int(i * step)] for i in range(max_points - 1)]
        rows = picked + [rows[-1]]
    return [dict(r, blocked=bool(r["blocked"])) for r in rows]


# ---------------------------------------------------------------- routes: devices
@app.post("/api/telemetry", status_code=204)
def post_telemetry(t: TelemetryIn, x_device_token: str = Header("")):
    device_auth(t.bin_id, x_device_token)
    ingest_telemetry(t)


@app.post("/api/events")
def post_event(e: EventIn, x_device_token: str = Header("")):
    device_auth(e.bin_id, x_device_token)
    return ingest_event(e)


@app.get("/api/device/commands")
def device_commands(bin_id: str, target: str = Query(pattern="^(gate|node)$"), x_device_token: str = Header("")):
    """裝置輪詢：取走自己桶子、自己負責的待執行指令（取走即標為 sent）。"""
    device_auth(bin_id, x_device_token)
    now = time.time()
    with db.conn() as c:
        c.execute("UPDATE commands SET status='expired', updated_at=? WHERE status='pending' AND created_at<?",
                  (now, now - COMMAND_TTL_S))
        rows = c.execute("SELECT id, cmd, args FROM commands WHERE bin_id=? AND target=? AND status='pending' "
                         "ORDER BY id", (bin_id, target)).fetchall()
        if rows:
            c.execute(f"UPDATE commands SET status='sent', updated_at=? WHERE id IN ({','.join('?' * len(rows))})",
                      (now, *[r["id"] for r in rows]))
    return [{"id": r["id"], "cmd": r["cmd"], "args": json.loads(r["args"] or "{}")} for r in rows]


@app.post("/api/device/commands/{cid}/result", status_code=204)
def device_command_result(cid: int, body: CommandResultIn, bin_id: str, x_device_token: str = Header("")):
    device_auth(bin_id, x_device_token)
    with db.conn() as c:
        n = c.execute("UPDATE commands SET status=?, result=?, updated_at=? WHERE id=? AND bin_id=?",
                      ("done" if body.ok else "failed", json.dumps(body.result, ensure_ascii=False),
                       time.time(), cid, bin_id)).rowcount
    if not n:
        raise HTTPException(404, "unknown command")


@app.post("/api/device/status", status_code=204)
def device_status(st: DeviceStatusIn, x_device_token: str = Header("")):
    """裝置上報各元件狀態（只存最新一筆）。"""
    device_auth(st.bin_id, x_device_token)
    data = json.dumps(st.components, ensure_ascii=False)
    if len(data.encode()) > STATUS_MAX_BYTES:
        raise HTTPException(413, f"status larger than {STATUS_MAX_BYTES} bytes")
    with db.conn() as c:
        c.execute("INSERT INTO device_status(bin_id,source,ts,data) VALUES(?,?,?,?) "
                  "ON CONFLICT(bin_id,source) DO UPDATE SET ts=excluded.ts, data=excluded.data",
                  (st.bin_id, st.source, time.time(), data))


# ---------------------------------------------------------------- routes: admin
@app.get("/api/bins/{bin_id}/status", dependencies=[Depends(require_admin)])
def bin_status(bin_id: str):
    """調度台：各元件最新狀態，含伺服器收到的時間 ts（前端據此判斷「未回報」）。"""
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM bins WHERE id=?", (bin_id,)).fetchone():
            raise HTTPException(404, "unknown bin")
        rows = c.execute("SELECT source, ts, data FROM device_status WHERE bin_id=?", (bin_id,)).fetchall()
    out = {"now": time.time(), "node": None, "gate": None}
    for r in rows:
        out[r["source"]] = {"ts": r["ts"], "components": json.loads(r["data"])}
    return out


@app.post("/api/bins/{bin_id}/commands", dependencies=[Depends(require_admin)])
def create_command(bin_id: str, body: CommandIn):
    target = COMMANDS.get(body.cmd)
    if not target:
        raise HTTPException(422, f"cmd must be one of {sorted(COMMANDS)}")
    args = {}
    if body.cmd == "gate_mode":
        if body.args.get("mode") not in GATE_MODES:
            raise HTTPException(422, f"mode must be one of {sorted(GATE_MODES)}")
        args = {"mode": body.args["mode"]}
    elif body.cmd == "gate_policy":
        if body.args.get("policy") not in GATE_POLICIES:
            raise HTTPException(422, f"policy must be one of {sorted(GATE_POLICIES)}")
        args = {"policy": body.args["policy"]}
    elif body.cmd == "lcd_text":
        text = str(body.args.get("text", "")).strip()
        if not text or len(text) > 32:
            raise HTTPException(422, "text must be 1–32 characters")
        if not all(" " <= ch <= "~" for ch in text):  # LCD1602 字庫只有英數字，中文會變亂碼
            raise HTTPException(422, "LCD 只能顯示英文、數字和半形符號")
        args = {"text": text}
    now = time.time()
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM bins WHERE id=?", (bin_id,)).fetchone():
            raise HTTPException(404, "unknown bin")
        cid = c.execute("INSERT INTO commands(bin_id,target,cmd,args,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (bin_id, target, body.cmd, json.dumps(args, ensure_ascii=False), now, now)).lastrowid
    return {"id": cid, "bin_id": bin_id, "target": target, "cmd": body.cmd, "args": args, "status": "pending"}


@app.get("/api/bins/{bin_id}/commands", dependencies=[Depends(require_admin)])
def list_commands(bin_id: str, limit: int = Query(10, ge=1, le=100)):
    now = time.time()
    with db.conn() as c:
        rows = c.execute("SELECT * FROM commands WHERE bin_id=? ORDER BY id DESC LIMIT ?", (bin_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r, args=json.loads(r["args"] or "{}"), result=json.loads(r["result"]) if r["result"] else None)
        if d["status"] == "pending" and r["created_at"] < now - COMMAND_TTL_S:
            d["status"] = "expired"
        out.append(d)
    return out


@app.post("/api/bins", dependencies=[Depends(require_admin)])
def create_bin(b: BinIn):
    token = secrets.token_urlsafe(24)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO bins(id,name,lat,lng,capacity_l,depth_mm,has_gate,token_hash,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (b.id, b.name, b.lat, b.lng, b.capacity_l, b.depth_mm, int(b.has_gate), _hash(token), time.time()))
    return {"id": b.id, "device_token": token}  # 只在建立時回傳一次


@app.get("/api/events", dependencies=[Depends(require_admin)])
def list_events(since: float = 0, limit: int = Query(100, le=1000)):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM events WHERE ts>? ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall()
    return [dict(r, detail=json.loads(r["detail"] or "{}")) for r in rows]


@app.get("/api/bins/{bin_id}/history", dependencies=[Depends(require_admin)])
def bin_history(bin_id: str, hours: float = Query(1, gt=0, le=HISTORY_MAX_HOURS),
                max_points: int = Query(300, ge=2, le=2000)):
    """調度台時間序列圖用。含 dist_mm 等原始讀值，所以只給 admin。"""
    return telemetry_history(bin_id, hours, max_points)


@app.post("/api/events/{event_id}/resolve", status_code=204, dependencies=[Depends(require_admin)])
def resolve_event(event_id: int):
    with db.conn() as c:
        c.execute("UPDATE events SET resolved=1 WHERE id=?", (event_id,))


@app.post("/api/dispatch/plan", dependencies=[Depends(require_admin)])
def dispatch_plan(p: PlanIn):
    plan = routing.plan_routes(DEPOT, _all_bins(), p.vehicles, p.vehicle_capacity_l, p.horizon_h)
    out = {"created_at": time.time(), "depot": {"lat": DEPOT[0], "lng": DEPOT[1]}, **plan.__dict__}
    with db.conn() as c:
        c.execute("INSERT INTO dispatch_plans(created_at, plan_json) VALUES(?,?)",
                  (out["created_at"], json.dumps(out, ensure_ascii=False)))
    return out


@app.get("/api/dispatch/latest", dependencies=[Depends(require_admin)])
def dispatch_latest():
    with db.conn() as c:
        r = c.execute("SELECT plan_json FROM dispatch_plans ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        raise HTTPException(404, "no plan yet")
    return json.loads(r["plan_json"])


# ---------------------------------------------------------------- routes: public
PUBLIC_FIELDS = ("id", "name", "lat", "lng", "state", "fill_pct", "weight_g", "hours_to_full",
                 "has_gate", "full_for_gate")


@app.get("/api/bins")
def list_bins():
    return [{k: b[k] for k in PUBLIC_FIELDS} for b in _all_bins()]


@app.get("/api/admin/bins", dependencies=[Depends(require_admin)])
def list_bins_admin():
    """調度台用：多了 last_seen、open_events、dist_mm、blocked 等不給市民看的欄位。"""
    return _all_bins()


def nearest_available(lat: float, lng: float, n: int = 3) -> list[dict]:
    cands = [b for b in _all_bins() if b["state"] in ("available", "near_full")]
    for b in cands:
        b["distance_m"] = round(logic.haversine_m(lat, lng, b["lat"], b["lng"]))
    cands.sort(key=lambda b: (b["distance_m"] + (150 if b["state"] == "near_full" else 0)))
    return [{k: b[k] for k in PUBLIC_FIELDS + ("distance_m",)} for b in cands[:n]]


@app.get("/api/nearest")
def api_nearest(lat: float, lng: float, n: int = Query(3, ge=1, le=10)):
    return nearest_available(lat, lng, n)


@app.get("/api/health")
def health():
    return {"ok": True, "ortools": routing.HAVE_ORTOOLS}


# ---------------------------------------------------------------- LINE
@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: str = Header("")):
    body = await request.body()
    if not line_bot.verify(body, x_line_signature):
        raise HTTPException(401, "bad signature")
    await line_bot.handle(json.loads(body), nearest_available)
    return {"ok": True}


# ---------------------------------------------------------------- static PWA
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root(request: Request):
    # 保留 ?demo=1、?live=1、?bin= 等參數，不然轉址後就失效
    q = request.url.query
    return RedirectResponse("/static/index.html" + (f"?{q}" if q else ""))
