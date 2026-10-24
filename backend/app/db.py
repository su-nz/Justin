"""SQLite 存取層。單檔資料庫，適合課堂 Demo 與小規模試點；正式部署可換 PostgreSQL/TimescaleDB。"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("SMARTBIN_DB", os.path.join(os.path.dirname(__file__), "..", "smartbin.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS bins (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    capacity_l   REAL NOT NULL DEFAULT 60,
    depth_mm     INTEGER NOT NULL DEFAULT 700,
    has_gate     INTEGER NOT NULL DEFAULT 0,
    token_hash   TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bin_id      TEXT NOT NULL REFERENCES bins(id),
    ts          REAL NOT NULL,
    fill_pct    REAL NOT NULL,
    dist_mm     INTEGER,
    weight_g    REAL,
    battery_mv  INTEGER,
    rssi        INTEGER,
    blocked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tel_bin_ts ON telemetry(bin_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bin_id      TEXT NOT NULL REFERENCES bins(id),
    ts          REAL NOT NULL,
    type        TEXT NOT NULL,
    confidence  REAL,
    detail      TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_evt_ts ON events(ts);
CREATE TABLE IF NOT EXISTS dispatch_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    plan_json   TEXT NOT NULL
);
"""


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()
