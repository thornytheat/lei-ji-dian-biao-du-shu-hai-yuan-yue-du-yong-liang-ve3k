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
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'published',
    current_version_id TEXT,
    revision   INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_versions (
    version_id       TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    correction_id    TEXT NOT NULL DEFAULT '',
    affected_months  TEXT NOT NULL DEFAULT '',
    payload          TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE TABLE IF NOT EXISTS correction_batches (
    correction_id    TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    applied_at       TEXT NOT NULL DEFAULT '',
    version_id       TEXT NOT NULL DEFAULT '',
    affected_months  TEXT NOT NULL DEFAULT '',
    request_hash     TEXT NOT NULL,
    payload          TEXT NOT NULL,
    error            TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, correction_id)
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
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


RUN_COLUMN_DEFS = {
    "status": "TEXT NOT NULL DEFAULT 'published'",
    "current_version_id": "TEXT",
    "revision": "INTEGER NOT NULL DEFAULT 1",
    "updated_at": "TEXT NOT NULL DEFAULT ''",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_runs(conn: sqlite3.Connection) -> None:
    """老库升级：runs 表补上版本指针列，并把已有的批次快照登记为第 1 版。

    旧批次快照不删除、不改写，登记成版本后仍可原样读取（可恢复）。
    """
    columns = _table_columns(conn, "runs")
    for name, definition in RUN_COLUMN_DEFS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    conn.execute("UPDATE runs SET updated_at = created_at WHERE updated_at = ''")

    legacy = conn.execute(
        "SELECT run_id, created_at, payload FROM runs WHERE current_version_id IS NULL"
    ).fetchall()
    for row in legacy:
        version_id = f"{row['run_id']}-v1"
        conn.execute(
            "INSERT OR IGNORE INTO run_versions "
            "(version_id, run_id, seq, status, created_at, correction_id, affected_months, payload) "
            "VALUES (?, ?, 1, 'published', ?, '', '', ?)",
            (version_id, row["run_id"], row["created_at"], row["payload"]),
        )
        conn.execute(
            "UPDATE runs SET current_version_id = ?, revision = 1 WHERE run_id = ?",
            (version_id, row["run_id"]),
        )


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_runs(conn)
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


def apply_reading_corrections(conn: sqlite3.Connection, corrections: list[dict], now: str) -> int:
    """把补录读数并入读数表（同键覆盖、缺键新增）。调用方负责事务边界。"""
    for row in corrections:
        conn.execute(
            "INSERT INTO readings (meter_id, read_at, reading_kwh, kind, imported_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(meter_id, read_at, kind) DO UPDATE SET "
            "reading_kwh = excluded.reading_kwh, imported_at = excluded.imported_at",
            (
                row["meter_id"],
                row["read_at"].isoformat(),
                float(row["reading_kwh"]),
                row["kind"],
                now,
            ),
        )
    return len(corrections)


def reading_span(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT MIN(read_at) AS lo, MAX(read_at) AS hi FROM readings").fetchone()
    return {"date_from": row["lo"], "date_to": row["hi"]}


def readings_fingerprint(conn: sqlite3.Connection) -> str:
    """读数集合指纹（与 publish.fingerprint 同口径），用于校验批次快照是否过期。"""
    import hashlib

    digest = hashlib.sha256()
    rows = conn.execute(
        "SELECT meter_id, read_at, kind, reading_kwh FROM readings "
        "ORDER BY meter_id, read_at, kind"
    ).fetchall()
    for row in rows:
        digest.update(
            f"{row['meter_id']}|{row['read_at']}|{row['kind']}|{float(row['reading_kwh']):.1f}\n".encode("utf-8")
        )
    return digest.hexdigest()


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


# --------------------------------------------------------------------------- 还原批次与版本


def save_run(conn: sqlite3.Connection, payload: dict) -> str:
    """新建批次并写入第 1 版快照，返回 version_id。"""
    run_id = payload["run_id"]
    version_id = f"{run_id}-v1"
    conn.execute(
        "INSERT INTO runs "
        "(run_id, created_at, month, meters, payload, status, current_version_id, revision, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'published', ?, 1, ?)",
        (
            run_id,
            payload["created_at"],
            payload.get("month") or "",
            ",".join(payload.get("selected") or []),
            json.dumps(payload, ensure_ascii=False),
            version_id,
            payload["created_at"],
        ),
    )
    conn.execute(
        "INSERT INTO run_versions "
        "(version_id, run_id, seq, status, created_at, correction_id, affected_months, payload) "
        "VALUES (?, ?, 1, 'published', ?, '', '', ?)",
        (version_id, run_id, payload["created_at"], json.dumps(payload, ensure_ascii=False)),
    )
    return version_id


def _normalize_ledger(ledger: dict) -> dict:
    """兼容旧快照：旧版 ledger 只存当月 rows 列表，读时统一包成月块。"""
    if not isinstance(ledger, dict):
        return {}
    normalized: dict[str, dict] = {}
    for month, value in ledger.items():
        if isinstance(value, list):
            normalized[month] = {
                "month": month,
                "rows": value,
                "evidence": [],
                "anomalies": [],
                "gaps": [],
                "changes": [],
                "rollovers": [],
                "checks": [],
                "review": [],
            }
        else:
            normalized[month] = value
    return normalized


def _run_payload(row: sqlite3.Row, payload: str | None = None) -> dict:
    data = json.loads(payload if payload is not None else row["payload"])
    data["ledger"] = _normalize_ledger(data.get("ledger") or {})
    data["revision"] = row["revision"] if "revision" in row.keys() else 1
    data["status"] = row["status"] if "status" in row.keys() else "published"
    if "current_version_id" in row.keys():
        data["version_id"] = row["current_version_id"] or data.get("version_id", "")
    if "updated_at" in row.keys() and row["updated_at"]:
        data["updated_at"] = row["updated_at"]
    return data


def load_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """读取批次当前对外发布的版本（不返回历史版本）。"""
    row = conn.execute(
        "SELECT payload, status, revision, current_version_id, updated_at FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return _run_payload(row) if row else None


def load_version_payload(conn: sqlite3.Connection, run_id: str, version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM run_versions WHERE run_id = ? AND version_id = ?",
        (run_id, version_id),
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["payload"])
    data["ledger"] = _normalize_ledger(data.get("ledger") or {})
    return data


def list_run_versions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT version_id, seq, status, created_at, correction_id, affected_months "
        "FROM run_versions WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [
        {
            "version_id": row["version_id"],
            "seq": row["seq"],
            "status": row["status"],
            "created_at": row["created_at"],
            "correction_id": row["correction_id"],
            "affected_months": [m for m in row["affected_months"].split(",") if m],
        }
        for row in rows
    ]


def version_info(conn: sqlite3.Connection, version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM run_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "version_id": row["version_id"],
        "run_id": row["run_id"],
        "seq": row["seq"],
        "status": row["status"],
        "created_at": row["created_at"],
        "correction_id": row["correction_id"],
        "affected_months": [m for m in row["affected_months"].split(",") if m],
    }


def next_version_seq(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS seq FROM run_versions WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(row["seq"]) + 1


def publish_version(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    payload: dict,
    correction_id: str,
    affected_months: list[str],
    now: str,
) -> dict:
    """把新版本一次性切换为当前发布版本；旧版本原样保留，随时可回读。"""
    seq = next_version_seq(conn, run_id)
    version_id = f"{run_id}-v{seq}"
    conn.execute(
        "INSERT INTO run_versions "
        "(version_id, run_id, seq, status, created_at, correction_id, affected_months, payload) "
        "VALUES (?, ?, ?, 'published', ?, ?, ?, ?)",
        (
            version_id,
            run_id,
            seq,
            now,
            correction_id,
            ",".join(affected_months),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.execute(
        "UPDATE runs SET payload = ?, status = 'published', current_version_id = ?, "
        "revision = ?, updated_at = ? WHERE run_id = ?",
        (json.dumps(payload, ensure_ascii=False), version_id, seq, now, run_id),
    )
    return {"version_id": version_id, "seq": seq}


def latest_run(conn: sqlite3.Connection, month: str | None = None) -> dict | None:
    if month:
        row = conn.execute(
            "SELECT payload, status, revision, current_version_id, updated_at FROM runs "
            "WHERE month = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload, status, revision, current_version_id, updated_at FROM runs "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return _run_payload(row) if row else None


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, month, meters, status, revision, current_version_id, updated_at "
        "FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "month": row["month"],
            "meters": row["meters"].split(",") if row["meters"] else [],
            "status": row["status"],
            "revision": row["revision"],
            "version_id": row["current_version_id"] or "",
            "updated_at": row["updated_at"] or row["created_at"],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 补录/更正批次


def save_correction(
    conn: sqlite3.Connection,
    *,
    correction_id: str,
    run_id: str,
    status: str,
    request_hash: str,
    payload: dict,
    affected_months: list[str],
    now: str,
    error: str = "",
) -> None:
    conn.execute(
        "INSERT INTO correction_batches "
        "(correction_id, run_id, status, created_at, applied_at, version_id, affected_months, "
        "request_hash, payload, error) VALUES (?, ?, ?, ?, '', '', ?, ?, ?, ?)",
        (
            correction_id,
            run_id,
            status,
            now,
            ",".join(affected_months),
            request_hash,
            json.dumps(payload, ensure_ascii=False),
            error,
        ),
    )


def load_correction(conn: sqlite3.Connection, correction_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM correction_batches WHERE correction_id = ?", (correction_id,)
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["payload"])
    data.update(
        {
            "correction_id": row["correction_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
            "version_id": row["version_id"],
            "affected_months": [m for m in row["affected_months"].split(",") if m],
            "request_hash": row["request_hash"],
            "error": row["error"],
        }
    )
    return data


def find_correction_by_hash(
    conn: sqlite3.Connection, run_id: str, request_hash: str
) -> dict | None:
    row = conn.execute(
        "SELECT correction_id FROM correction_batches "
        "WHERE run_id = ? AND request_hash = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (run_id, request_hash),
    ).fetchone()
    return load_correction(conn, row["correction_id"]) if row else None


def update_correction(
    conn: sqlite3.Connection,
    correction_id: str,
    *,
    status: str,
    applied_at: str = "",
    version_id: str = "",
    error: str = "",
) -> None:
    conn.execute(
        "UPDATE correction_batches SET status = ?, applied_at = ?, version_id = ?, error = ? "
        "WHERE correction_id = ?",
        (status, applied_at, version_id, error, correction_id),
    )


def list_corrections(conn: sqlite3.Connection, run_id: str | None = None, limit: int = 20) -> list[dict]:
    if run_id:
        rows = conn.execute(
            "SELECT correction_id FROM correction_batches WHERE run_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT correction_id FROM correction_batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [load_correction(conn, row["correction_id"]) for row in rows]


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
