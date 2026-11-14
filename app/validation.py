"""分时校验与缺口判定（纯函数）。

- 峰谷一致性：peak + valley ≈ total，容差 0.5%（相对总量），超差记异常但仍输出数值。
- 缺口：相邻读数间隔 > 31 天即缺口，只报区间与建议抄表日，**不做任何插值**。
"""

from __future__ import annotations

from datetime import date, timedelta

TOU_TOLERANCE = 0.005


def time_of_use_check(
    total: float | None,
    peak: float | None,
    valley: float | None,
    tolerance: float = TOU_TOLERANCE,
) -> dict:
    """峰 + 谷 与总量比对。返回 ok/偏差/容差/说明，超差时 ok=False 但数值照常输出。"""
    if total is None or peak is None or valley is None:
        return {
            "ok": False,
            "status": "missing",
            "total": total,
            "peak": peak,
            "valley": valley,
            "sum": None,
            "diff": None,
            "tolerance": tolerance,
            "allowed": None,
            "deviation_ratio": None,
            "note": "缺少 total/peak/valley 之一，无法做分时校验",
        }
    total_v = round(float(total), 2)
    peak_v = round(float(peak), 2)
    valley_v = round(float(valley), 2)
    summed = round(peak_v + valley_v, 2)
    diff = round(summed - total_v, 2)
    allowed = round(abs(total_v) * tolerance, 4)
    ratio = None if abs(total_v) < 1e-9 else round(diff / total_v, 6)
    ok = abs(diff) <= allowed + 1e-9
    return {
        "ok": ok,
        "status": "ok" if ok else "mismatch",
        "total": total_v,
        "peak": peak_v,
        "valley": valley_v,
        "sum": summed,
        "diff": diff,
        "tolerance": tolerance,
        "allowed": allowed,
        "deviation_ratio": ratio,
        "note": (
            f"峰 {peak_v:g} + 谷 {valley_v:g} = {summed:g}，总量 {total_v:g}，"
            f"偏差 {diff:+g} kWh（容差 ±{allowed:g}，"
            f"{'通过' if ok else '超差，已记异常但仍输出'}）"
        ),
    }


def gap_missing_range(gap: dict) -> tuple[date, date]:
    """缺口缺失区间：首尾读数之间（不含两端）。"""
    start = gap["start"] if isinstance(gap["start"], date) else date.fromisoformat(str(gap["start"]))
    end = gap["end"] if isinstance(gap["end"], date) else date.fromisoformat(str(gap["end"]))
    return start + timedelta(days=1), end


def gap_days_in_month(gap: dict, month_start: date, month_end: date) -> int:
    """缺口缺失天数与该日历月的重叠天数。"""
    start, end = gap_missing_range(gap)
    lo = max(start, month_start)
    hi = min(end, month_end)
    return max(0, (hi - lo).days)


def overlaps(start: date, end: date, month_start: date, month_end: date) -> bool:
    return start < month_end and end > month_start


def coverage_note(usage: float, days: int, missing_days: int) -> str:
    """覆盖说明：缺口期间没有读数，因此日均值仅按实际区间天数给出，不插值。"""
    if days <= 0:
        return "区间天数为 0"
    avg = round(usage / days, 2)
    if missing_days > 0:
        return f"区间 {days} 天（含缺口缺失 {missing_days} 天），日均 {avg} kWh，缺口不插值"
    return f"区间 {days} 天，日均 {avg} kWh"
