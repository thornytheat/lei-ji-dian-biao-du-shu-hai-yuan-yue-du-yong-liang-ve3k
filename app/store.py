"""SQLite 访问层：读数、换表记录、表计台账、还原批次、事件。

命名卷 metercalc-data 挂到 /app/data；重建数据：docker compose down -v 后重跑。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    full_scale REAL NOT NULL DEFAULT 99999.9,
    ct_ratio   REAL NOT NULL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    read_at     TEXT NOT NULL,
    reading_kwh REAL NOT NULL,
    kind        TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (meter_id, read_at, kind)
);
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id    TEXT NOT NULL,
    change_date TEXT NOT NULL,
    payload     TEXT NOT NULL,
    UNIQUE (meter_id, change_date)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    month      TEXT NOT NULL,
    meters     TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- 表计台账


def upsert_meters(conn: sqlite3.Connection, meters: list[dict]) -> None:
    for meter in meters:
        conn.execute(
            """
            INSERT INTO meters (meter_id, name, location, full_scale, ct_ratio)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meter_id) DO UPDATE SET
                name = excluded.name,
                location = excluded.location,
                full_scale = excluded.full_scale,
                ct_ratio = excluded.ct_ratio
            """,
            (
                meter["meter_id"],
                meter.get("name", ""),
                meter.get("location", ""),
                float(meter.get("full_scale") or settings.DEFAULT_FULL_SCALE),
                float(meter.get("ct_ratio") or 1.0),
            ),
        )


def all_meters(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM meters ORDER BY meter_id").fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "name": row["name"],
            "location": row["location"],
            "full_scale": float(row["full_scale"]),
            "ct_ratio": float(row["ct_ratio"]),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 读数


def replace_readings(conn: sqlite3.Connection, rows: list[dict], now: str) -> int:
    conn.execute("DELETE FROM readings")
    conn.executemany(
        "INSERT INTO readings (meter_id, read_at, reading_kwh, kind, imported_at) VALUES (?, ?, ?, ?, ?)",
        [
            (row["meter_id"], row["read_at"].isoformat(), float(row["reading_kwh"]), row["kind"], now)
            for row in rows
        ],
    )
    return len(rows)


def all_readings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT meter_id, read_at, reading_kwh, kind FROM readings ORDER BY meter_id, read_at, kind"
    ).fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "read_at": date.fromisoformat(row["read_at"]),
            "reading_kwh": float(row["reading_kwh"]),
            "kind": row["kind"],
        }
        for row in rows
    ]


def count_readings(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()
    return int(row["c"])


def reading_span(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(read_at) AS lo, MAX(read_at) AS hi FROM readings").fetchone()
    return {"date_from": row["lo"], "date_to": row["hi"]}


# --------------------------------------------------------------------------- 换表记录


def replace_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    conn.execute("DELETE FROM changes")
    for change in changes:
        conn.execute(
            "INSERT INTO changes (meter_id, change_date, payload) VALUES (?, ?, ?)",
            (change["meter_id"], change["change_date"], json.dumps(change, ensure_ascii=False)),
        )
    return len(changes)


def all_changes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT payload FROM changes ORDER BY change_date").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def count_changes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM changes").fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------- 还原批次


def save_run(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, created_at, month, meters, payload) VALUES (?, ?, ?, ?, ?)",
        (
            payload["run_id"],
            payload["created_at"],
            payload.get("month") or "",
            ",".join(payload.get("selected") or []),
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def load_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_run(conn: sqlite3.Connection, month: str | None = None) -> dict | None:
    if month:
        row = conn.execute(
            "SELECT payload FROM runs WHERE month = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, month, meters FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "month": row["month"],
            "meters": row["meters"].split(",") if row["meters"] else [],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 事件 / 元数据


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
