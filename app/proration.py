"""月度分摊：跨月区间按天数比例分摊到各自日历月，保留 2 位小数且合计守恒。

用「分」为整数单位 + 最大余数法分配，保证 sum(分摊) 与区间用量（2 位小数）完全相等，
不出现四舍五入导致的 0.01 丢量。
"""

from __future__ import annotations

import math
from datetime import date, timedelta


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_month(text: str) -> tuple[date, date]:
    """'YYYY-MM' → (当月首日, 次月首日)。非法格式抛 ValueError。"""
    raw = (text or "").strip()
    if len(raw) != 7 or raw[4] != "-":
        raise ValueError(f"月份格式应为 YYYY-MM，实际 {raw!r}")
    try:
        year, month = int(raw[0:4]), int(raw[5:7])
        start = date(year, month, 1)
    except ValueError as exc:
        raise ValueError(f"月份 {raw!r} 不是有效的日历月") from exc
    end = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return start, end


def next_month(month: str) -> str:
    start, _ = parse_month(month)
    return month_of(start + timedelta(days=32))


def prev_month(month: str) -> str:
    start, _ = parse_month(month)
    return month_of(start - timedelta(days=1))


def split_months(start: date, end: date) -> list[dict]:
    """把 [start, end) 拆成按日历月的天数桶。"""
    if end <= start:
        return []
    buckets: list[dict] = []
    cursor = start
    while cursor < end:
        boundary = parse_month(month_of(cursor))[1]
        stop = min(boundary, end)
        buckets.append(
            {
                "month": month_of(cursor),
                "days": (stop - cursor).days,
                "from": cursor,
                "to": stop,
            }
        )
        cursor = stop
    return buckets


def prorate(start: date, end: date, usage: float) -> dict:
    """按天数比例分摊，返回每个月的份额（2 位小数，合计守恒）。"""
    days_total = (end - start).days
    cents_total = int(round(float(usage) * 100))
    if days_total <= 0:
        return {
            "usage": round(cents_total / 100.0, 2),
            "days": 0,
            "parts": [],
            "residual_cents": 0,
            "formula": "区间天数为 0，无法分摊",
            "balanced": True,
        }

    buckets = split_months(start, end)
    raw = [cents_total * bucket["days"] / days_total for bucket in buckets]
    floors = [int(math.floor(value)) for value in raw]
    residual = cents_total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for index in order[: max(residual, 0)]:
        floors[index] += 1

    parts = []
    for bucket, cents in zip(buckets, floors):
        parts.append(
            {
                "month": bucket["month"],
                "days": bucket["days"],
                "share": round(cents / 100.0, 2),
                "weight": round(bucket["days"] / days_total, 6),
                "from": bucket["from"].isoformat(),
                "to": bucket["to"].isoformat(),
            }
        )
    allocated = round(sum(part["share"] for part in parts), 2)
    usage_2 = round(cents_total / 100.0, 2)
    formula_parts = " + ".join(
        f"{part['month']} {part['days']}/{days_total}×{usage_2:g}={part['share']:g}" for part in parts
    )
    return {
        "usage": usage_2,
        "days": days_total,
        "parts": parts,
        "residual_cents": residual,
        "formula": f"区间 {start.isoformat()} → {end.isoformat()} 共 {days_total} 天，用量 {usage_2:g} kWh 分摊：{formula_parts}",
        "balanced": abs(allocated - usage_2) <= 0.01,
        "allocated": allocated,
    }


def allocate(intervals: list[dict]) -> dict:
    """把一组区间按（月份）汇总，并保留每个区间的分摊依据。"""
    by_month: dict[str, float] = {}
    evidence: list[dict] = []
    for interval in intervals:
        usage = interval.get("usage")
        if usage is None:
            continue
        split = prorate(interval["start"], interval["end"], usage)
        interval["proration"] = split
        for part in split["parts"]:
            by_month[part["month"]] = round(by_month.get(part["month"], 0.0) + part["share"], 2)
        evidence.append(
            {
                "meter_id": interval["meter_id"],
                "kind": interval["kind"],
                "start": interval["start"].isoformat(),
                "end": interval["end"].isoformat(),
                "days": split["days"],
                "usage": split["usage"],
                "status": interval.get("status"),
                "formula": interval.get("formula", ""),
                "proration": split,
                "gap": interval.get("gap", False),
            }
        )
    return {"by_month": by_month, "evidence": evidence}


def reconcile(parts_total: float, target: float) -> float:
    """守恒校验辅助：返回差额（正数表示分摊结果偏大）。"""
    return round(parts_total - target, 4)
