"""读数导入：CSV 严格解析、逐行校验、按（表号, 电量类型）分组排序、缺口检测。

严格导入：任意一行不合法即整体拒绝（返回全部错误位置），避免半脏数据进入还原流程。
"""

from __future__ import annotations

import csv
import io
from datetime import date

KINDS = ("total", "peak", "valley")
KIND_LABELS = {"total": "总电量", "peak": "峰段", "valley": "谷段"}
REQUIRED_COLUMNS = ("meter_id", "read_at", "reading_kwh", "kind")
MAX_PROBLEMS = 40


class ReadingError(Exception):
    """读数文件结构性/字段级错误：一次性带出全部问题。"""

    def __init__(self, message: str, problems: list[dict] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []


def parse_date(text: str) -> date:
    """严格 ISO 日期，拒绝 2026-02-30 这类日历上不存在的日期。"""
    raw = (text or "").strip()
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError(f"日期格式应为 YYYY-MM-DD，实际 {raw!r}")
    try:
        return date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"日期 {raw!r} 不是有效日历日期") from exc


def parse_reading(text: str) -> float:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("读数不能为空")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"读数 {raw!r} 不是数字") from exc
    if value != value:  # NaN
        raise ValueError("读数不能是 NaN")
    if value < 0:
        raise ValueError(f"读数不能为负：{value}")
    return round(value, 1)


def parse_csv(text: str) -> dict:
    """解析读数 CSV。返回 {rows, stats}；有问题时抛 ReadingError（携全部问题）。"""
    if not (text or "").strip():
        raise ReadingError("读数文件为空", [{"line": 0, "field": "file", "message": "文件内容为空"}])
    sample = text[1:] if text.startswith("\ufeff") else text
    reader = csv.DictReader(io.StringIO(sample))
    header = [(name or "").strip() for name in (reader.fieldnames or [])]
    missing = [col for col in REQUIRED_COLUMNS if col not in header]
    if missing:
        raise ReadingError(
            f"缺少必需列：{', '.join(missing)}",
            [{"line": 1, "field": "header", "message": f"缺少列 {col}"} for col in missing],
        )
    reader.fieldnames = header

    rows: list[dict] = []
    problems: list[dict] = []
    seen: dict[tuple[str, str, str], int] = {}

    for index, raw in enumerate(reader, start=2):
        if raw is None or all((v or "").strip() == "" for v in raw.values() if isinstance(v, str)):
            continue  # 空行跳过
        meter_id = (raw.get("meter_id") or "").strip()
        kind = (raw.get("kind") or "").strip().lower()
        if not meter_id:
            problems.append({"line": index, "field": "meter_id", "message": "表号为空"})
            continue
        if kind not in KINDS:
            problems.append(
                {"line": index, "field": "kind", "message": f"未知电量类型 {kind!r}，只支持 total/peak/valley"}
            )
            continue
        try:
            read_at = parse_date(raw.get("read_at") or "")
        except ValueError as exc:
            problems.append({"line": index, "field": "read_at", "message": str(exc)})
            continue
        try:
            value = parse_reading(raw.get("reading_kwh") or "")
        except ValueError as exc:
            problems.append({"line": index, "field": "reading_kwh", "message": str(exc)})
            continue
        key = (meter_id, read_at.isoformat(), kind)
        if key in seen:
            problems.append(
                {
                    "line": index,
                    "field": "read_at",
                    "message": f"重复读数：{meter_id} {read_at} {kind} 已在第 {seen[key]} 行出现",
                }
            )
            continue
        seen[key] = index
        rows.append(
            {"meter_id": meter_id, "read_at": read_at, "reading_kwh": value, "kind": kind, "line": index}
        )

    if not rows and not problems:
        problems.append({"line": 0, "field": "file", "message": "没有可解析的数据行"})
    if problems:
        head = problems[:MAX_PROBLEMS]
        raise ReadingError(f"读数文件有 {len(problems)} 处错误，已拒绝导入", head)

    meters = sorted({row["meter_id"] for row in rows})
    dates = sorted({row["read_at"] for row in rows})
    return {
        "rows": rows,
        "stats": {
            "rows": len(rows),
            "meters": meters,
            "date_from": dates[0].isoformat(),
            "date_to": dates[-1].isoformat(),
            "kinds": sorted({row["kind"] for row in rows}),
        },
    }


def group_series(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """按（表号, 类型）分组并按日期升序排列。"""
    series: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        series.setdefault((row["meter_id"], row["kind"]), []).append(
            {"read_at": row["read_at"], "value": row["reading_kwh"], "line": row.get("line")}
        )
    for points in series.values():
        points.sort(key=lambda item: item["read_at"])
    return series


def detect_gaps(points: list[dict], max_gap_days: int) -> list[dict]:
    """相邻读数间隔 > max_gap_days 即缺口；只报缺口，不做任何插值。"""
    gaps: list[dict] = []
    for prev, nxt in zip(points, points[1:]):
        span = (nxt["read_at"] - prev["read_at"]).days
        if span <= max_gap_days:
            continue
        missing = span - 1
        mid = prev["read_at"] + (nxt["read_at"] - prev["read_at"]) / 2
        gaps.append(
            {
                "start": prev["read_at"],
                "end": nxt["read_at"],
                "span_days": span,
                "missing_days": missing,
                "suggested_read_at": mid.isoformat(),
                "note": (
                    f"相邻读数间隔 {span} 天 > {max_gap_days} 天，缺口 {missing} 天；"
                    f"建议在 {mid.isoformat()} 前后补抄一次，缺口不插值"
                ),
                "prev_value": prev["value"],
                "next_value": nxt["value"],
            }
        )
    return gaps


def meter_gaps(series: dict[tuple[str, str], list[dict]], max_gap_days: int) -> dict[str, list[dict]]:
    """按表汇总缺口（以 total 序列为准，峰谷序列同源不重复报）。"""
    result: dict[str, list[dict]] = {}
    for (meter_id, kind), points in series.items():
        if kind != "total":
            continue
        found = detect_gaps(points, max_gap_days)
        if found:
            result[meter_id] = found
    return result
