"""抄表补录 / 迟到更正：整批校验、受影响月份计算、批次幂等指纹。

补录是**覆盖同一（表号, 日期, 电量类型）读数**的迟到更正，而不是替换整份读数文件：

1. 整批字段级校验（表号/日期/类型/数值/批内重复），任一问题即整批拒绝；
2. 在内存里把更正合并进现有读数序列，先完整还原一次，确认新结果可生成；
3. 只标记更正点两侧区间覆盖到的月份为「受影响月份」，发布时只替换这些月份，
   其余月份沿用旧版本快照。

重复提交同一批（内容相同）用 request_hash 识别，不会生成新的批次或版本。
"""

from __future__ import annotations

import bisect
import hashlib
import json

from . import proration, readings

MAX_PROBLEMS = 40
ALIAS_KEYS = ("readings", "corrections", "records", "rows")


def extract_rows(payload: dict) -> list:
    """从请求体取更正明细；兼容 readings / corrections / records / rows 几种叫法。"""
    for key in ALIAS_KEYS:
        value = payload.get(key)
        if value is not None:
            return value
    return []


def normalize(raw_rows, *, known_meters: set[str]) -> tuple[list[dict], list[dict]]:
    """完整校验一整批更正。返回 (normalized, problems)，problems 非空时不得落库。"""
    if not isinstance(raw_rows, list):
        return [], [{"line": 0, "field": "readings", "message": "更正明细必须是数组"}]
    if not raw_rows:
        return [], [{"line": 0, "field": "readings", "message": "更正明细为空，至少提供一条补录读数"}]

    normalized: list[dict] = []
    problems: list[dict] = []
    seen: dict[tuple[str, str, str], int] = {}

    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            problems.append({"line": index, "field": "readings", "message": "每条更正必须是对象"})
            continue
        meter_id = str(raw.get("meter_id") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if not meter_id:
            problems.append({"line": index, "field": "meter_id", "message": "表号为空"})
            continue
        if known_meters and meter_id not in known_meters:
            problems.append(
                {
                    "line": index,
                    "field": "meter_id",
                    "message": f"表号 {meter_id} 不存在；可选表号：{', '.join(sorted(known_meters))}",
                }
            )
            continue
        if kind not in readings.KINDS:
            problems.append(
                {
                    "line": index,
                    "field": "kind",
                    "message": f"未知电量类型 {kind!r}，只支持 total/peak/valley",
                }
            )
            continue
        try:
            read_at = readings.parse_date(str(raw.get("read_at") or ""))
        except ValueError as exc:
            problems.append({"line": index, "field": "read_at", "message": str(exc)})
            continue
        try:
            value = readings.parse_reading(str(raw.get("reading_kwh", "")))
        except ValueError as exc:
            problems.append({"line": index, "field": "reading_kwh", "message": str(exc)})
            continue
        key = (meter_id, read_at.isoformat(), kind)
        if key in seen:
            problems.append(
                {
                    "line": index,
                    "field": "read_at",
                    "message": f"同一批内重复更正：{meter_id} {read_at} {kind} 已在第 {seen[key]} 条出现",
                }
            )
            continue
        seen[key] = index
        normalized.append(
            {"meter_id": meter_id, "read_at": read_at, "reading_kwh": value, "kind": kind, "line": index}
        )

    if len(problems) > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS]
    return normalized, problems


def serialize(rows: list[dict]) -> list[dict]:
    """规范化为可入库/可回放的 JSON 明细。"""
    return [
        {
            "meter_id": row["meter_id"],
            "read_at": row["read_at"].isoformat(),
            "reading_kwh": float(row["reading_kwh"]),
            "kind": row["kind"],
        }
        for row in rows
    ]


def request_hash(run_id: str, rows: list[dict]) -> str:
    """同一批更正的内容指纹：与提交顺序无关，重复提交得到同一个值。"""
    canonical = sorted(
        (
            row["meter_id"],
            row["read_at"].isoformat(),
            row["kind"],
            f"{float(row['reading_kwh']):.1f}",
        )
        for row in rows
    )
    raw = json.dumps({"run_id": run_id, "readings": canonical}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_to_rows(existing_rows: list[dict], corrections: list[dict]) -> tuple[list[dict], int]:
    """在内存里用更正覆盖原读数（同键覆盖、缺键新增）。返回 (新序列, 实际改变条数)。"""
    index = {
        (row["meter_id"], row["read_at"].isoformat(), row["kind"]): row for row in existing_rows
    }
    changed = 0
    for correction in corrections:
        key = (correction["meter_id"], correction["read_at"].isoformat(), correction["kind"])
        current = index.get(key)
        if current is None:
            index[key] = {
                "meter_id": correction["meter_id"],
                "read_at": correction["read_at"],
                "reading_kwh": correction["reading_kwh"],
                "kind": correction["kind"],
            }
            changed += 1
        elif round(float(current["reading_kwh"]), 1) != round(float(correction["reading_kwh"]), 1):
            index[key] = dict(current, reading_kwh=correction["reading_kwh"])
            changed += 1
    rows = sorted(index.values(), key=lambda row: (row["meter_id"], row["read_at"], row["kind"]))
    return rows, changed


def affected_months(
    corrections: list[dict],
    existing_rows: list[dict],
    *,
    max_gap_days: int = 31,
) -> list[str]:
    """更正会改动哪些月份的台账。

    更正点只影响它两侧的区间：`[前一条读数, 更正日)` 与 `[更正日, 后一条读数)`。
    这些区间按日历月分摊覆盖到的月份即为受影响月份；更正日本身所在月保守计入。
    缺口判定同样只依赖相邻日期，因此不会波及区间之外的月份。
    """
    series: dict[tuple[str, str], dict[date, float]] = {}
    for row in existing_rows:
        series.setdefault((row["meter_id"], row["kind"]), {})[row["read_at"]] = float(
            row["reading_kwh"]
        )
    for correction in corrections:
        series.setdefault((correction["meter_id"], correction["kind"]), {})[
            correction["read_at"]
        ] = float(correction["reading_kwh"])

    months: set[str] = set()
    for correction in corrections:
        dates = sorted(series[(correction["meter_id"], correction["kind"])])
        day = correction["read_at"]
        position = bisect.bisect_left(dates, day)
        prev_day = dates[position - 1] if position > 0 else None
        next_day = dates[position + 1] if position + 1 < len(dates) else None

        if prev_day is not None and next_day is not None:
            start, end = prev_day, next_day
        elif prev_day is not None:
            start, end = prev_day, day
        elif next_day is not None:
            start, end = day, next_day
        else:
            start, end = day, day

        if end > start:
            for bucket in proration.split_months(start, end):
                months.add(bucket["month"])
        months.add(proration.month_of(day))
    return sorted(months)
