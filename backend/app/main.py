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
    with db.conn() as c:
        rows = c.execute("SELECT ts, fill_pct, blocked FROM telemetry WHERE bin_id=? AND ts>? ORDER BY ts",
                         (bin_id, now - 24 * 3600)).fetchall()
    if not rows:
        return logic.status_of([], False, now=now)
    samples = [(r["ts"], r["fill_pct"]) for r in rows]
    prev = None  # 依時間跑一遍狀態機，讓遲滯生效
    for _, f in samples[-12:-1]:
        prev = logic.classify(f, prev)
    return logic.status_of(samples, bool(rows[-1]["blocked"]), prev, now)


def _all_bins() -> list[dict]:
    with db.conn() as c:
        bins = [dict(r) for r in c.execute(
            "SELECT id,name,lat,lng,capacity_l,depth_mm,has_gate FROM bins ORDER BY id")]
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
                 open_events=open_ev.get(b["id"], []), has_gate=bool(b["has_gate"]))
    return bins


# ---------------------------------------------------------------- routes: devices
@app.post("/api/telemetry", status_code=204)
def post_telemetry(t: TelemetryIn, x_device_token: str = Header("")):
    device_auth(t.bin_id, x_device_token)
    ingest_telemetry(t)


@app.post("/api/events")
def post_event(e: EventIn, x_device_token: str = Header("")):
    device_auth(e.bin_id, x_device_token)
    return ingest_event(e)


# ---------------------------------------------------------------- routes: admin
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
PUBLIC_FIELDS = ("id", "name", "lat", "lng", "state", "fill_pct", "hours_to_full", "has_gate")


@app.get("/api/bins")
def list_bins():
    return [{k: b[k] for k in PUBLIC_FIELDS} for b in _all_bins()]


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
def root():
    return RedirectResponse("/static/index.html")
