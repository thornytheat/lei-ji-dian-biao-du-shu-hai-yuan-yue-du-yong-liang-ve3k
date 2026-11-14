"""路径与常量。数据目录可用 METERCALC_DATA_DIR 覆盖（测试用 tmp 目录）。"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

DATA_DIR = Path(os.environ.get("METERCALC_DATA_DIR") or (PROJECT_DIR / "data"))
SAMPLE_DIR = Path(os.environ.get("METERCALC_SAMPLE_DIR") or (PROJECT_DIR / "sample_data"))
READINGS_SAMPLE = SAMPLE_DIR / "readings.csv"
METER_CHANGES_SAMPLE = SAMPLE_DIR / "meter_changes.json"
METERS_SAMPLE = SAMPLE_DIR / "meters.json"
DB_PATH = Path(os.environ.get("METERCALC_DB") or (DATA_DIR / "metercalc.sqlite3"))

APP_NAME = "园区电表累计读数还原"
APP_SHORT = "metercalc"
SEED_VERSION = "sample-2025-04..2026-03-v1"
SEED_SOURCE = "sample_data/readings.csv + meter_changes.json + meters.json"

# 量程（回绕判定用），可由请求覆盖或按表配置覆盖
DEFAULT_FULL_SCALE = 99999.9
# 相邻读数间隔超过该天数即缺口，禁止插值
MAX_GAP_DAYS = 31
# 分时一致性容差
TOU_TOLERANCE = 0.005
