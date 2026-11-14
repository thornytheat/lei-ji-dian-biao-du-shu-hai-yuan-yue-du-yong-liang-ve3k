"""幂等 seed：只在空库或样例版本变化时写入；重复启动不产生脏数据。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import readings as readings_mod
from . import settings, store


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def load_sample_files(
    readings_path: Path | None = None,
    changes_path: Path | None = None,
    meters_path: Path | None = None,
) -> dict:
    readings_path = Path(readings_path or settings.READINGS_SAMPLE)
    changes_path = Path(changes_path or settings.METER_CHANGES_SAMPLE)
    meters_path = Path(meters_path or settings.METERS_SAMPLE)
    parsed = readings_mod.parse_csv(readings_path.read_text(encoding="utf-8"))
    changes = json.loads(changes_path.read_text(encoding="utf-8")).get("changes", [])
    meters = json.loads(meters_path.read_text(encoding="utf-8")).get("meters", [])
    return {"rows": parsed["rows"], "stats": parsed["stats"], "changes": changes, "meters": meters}


def seed(
    conn: sqlite3.Connection,
    sample: dict | None = None,
    seed_version: str | None = None,
    force: bool = False,
) -> dict:
    store.init_schema(conn)
    sample = sample or load_sample_files()
    version = seed_version or settings.SEED_VERSION
    current_version = store.get_meta(conn, "seed_version")
    current_rows = store.count_readings(conn)
    if not force and current_version == version and current_rows == len(sample["rows"]) and current_rows > 0:
        return {
            "seeded": False,
            "reason": "已是最新样例版本，跳过（幂等）",
            "seed_version": version,
            "rows": current_rows,
        }

    now = now_iso()
    store.replace_readings(conn, sample["rows"], now)
    store.replace_changes(conn, sample["changes"])
    store.upsert_meters(conn, sample["meters"])
    store.set_meta(conn, "seed_version", version)
    store.set_meta(conn, "seeded_at", now)
    store.add_event(
        conn,
        "seed",
        f"导入样例读数 {len(sample['rows'])} 行 / {len(sample['stats']['meters'])} 块表 / {len(sample['changes'])} 条换表记录",
        {"seed_version": version, "rows": len(sample["rows"]), "stats": sample["stats"]},
        now,
    )
    conn.commit()
    return {
        "seeded": True,
        "reason": "样例已按当前版本重新导入（DELETE+INSERT 单事务，行数不变）" if force else "样例数据已写入",
        "seed_version": version,
        "rows": len(sample["rows"]),
        "stats": sample["stats"],
    }
