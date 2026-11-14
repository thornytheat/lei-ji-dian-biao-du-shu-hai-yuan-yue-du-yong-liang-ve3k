"""用量还原：区间用量、换表分段、回绕识别、无法判定异常隔离。

规则（对每个 表号 × 电量类型 的读数序列逐对应用）：

1. 后读数 >= 前读数：usage = 后 − 前（正常）。
2. 后读数 <  前读数：
   a. 该区间被换表记录覆盖（start < 换表日 <= end）→ 两表分段求和：
      旧表尾段 = 拆表读数 − 前读数；新表首段 = 后读数 − 装表读数；usage = 两段之和。
   b. 否则若 前读数 接近量程顶部（>= 量程 × 0.8）且 后读数 很小（<= 量程 × 0.2）
      → 回绕：usage = 量程 − 前读数 + 后读数。
   c. 两者都不成立 → anomaly：隔离，usage 记 None，不参与汇总。
"""

from __future__ import annotations

from datetime import date

DEFAULT_FULL_SCALE = 99999.9
ROLLOVER_PREV_RATIO = 0.8
ROLLOVER_NEXT_RATIO = 0.2


def _t(value: float) -> float:
    return round(float(value), 2)


def find_change(changes: list[dict], meter_id: str, start: date, end: date) -> dict | None:
    """找到覆盖该区间的换表记录（start < 换表日 <= end）。"""
    for change in changes or []:
        if change.get("meter_id") != meter_id:
            continue
        try:
            when = date.fromisoformat(str(change.get("change_date")))
        except (TypeError, ValueError):
            continue
        if start < when <= end:
            return change
    return None


def _segment_readings(change: dict, kind: str) -> tuple[float, float] | None:
    segments = change.get("segments") or {}
    seg = segments.get(kind)
    if not seg:
        return None
    old_final = seg.get("old_final")
    new_initial = seg.get("new_initial", 0.0)
    if old_final is None:
        return None
    try:
        return float(old_final), float(new_initial)
    except (TypeError, ValueError):
        return None


def interval_usage(
    prev: dict,
    nxt: dict,
    *,
    meter_id: str,
    kind: str,
    full_scale: float,
    change: dict | None,
) -> dict:
    """单个相邻读数区间的用量还原。返回带 status/usage/formula 的证据。"""
    start, end = prev["read_at"], nxt["read_at"]
    before, after = float(prev["value"]), float(nxt["value"])
    days = (end - start).days
    base = {
        "meter_id": meter_id,
        "kind": kind,
        "start": start,
        "end": end,
        "days": days,
        "prev_value": _t(before),
        "next_value": _t(after),
        "delta_raw": _t(after - before),
        "full_scale": _t(full_scale),
        "segments": [],
    }

    if after >= before:
        base.update(
            status="normal",
            usage=_t(after - before),
            formula=f"后读数 {after:g} − 前读数 {before:g} = {after - before:g}",
            reason="",
        )
        return base

    # a) 换表覆盖
    if change is not None:
        pair = _segment_readings(change, kind)
        if pair is None:
            base.update(
                status="anomaly",
                usage=None,
                formula="",
                reason=f"读数回退且换表记录缺少 {kind} 的拆表/装表读数，无法分段",
            )
            return base
        old_final, new_initial = pair
        old_seg = _t(old_final - before)
        new_seg = _t(after - new_initial)
        old_days = (date.fromisoformat(str(change["change_date"])) - start).days
        new_days = days - old_days
        if old_final < before or after < new_initial or old_seg < 0 or new_seg < 0:
            base.update(
                status="anomaly",
                usage=None,
                formula="",
                reason=(
                    f"换表记录与读数不自洽：拆表读数 {old_final:g} < 前读数 {before:g} "
                    f"或 后读数 {after:g} < 装表读数 {new_initial:g}"
                ),
            )
            return base
        total = _t(old_seg + new_seg)
        base.update(
            status="meter_change",
            usage=total,
            formula=(
                f"换表分段（{change.get('change_date')} {change.get('old_meter_no', '旧表')}"
                f" → {change.get('new_meter_no', '新表')}）："
                f"旧表尾段 {old_final:g} − {before:g} = {old_seg:g}；"
                f"新表首段 {after:g} − {new_initial:g} = {new_seg:g}；合计 {total:g}"
            ),
            reason="",
            change={
                "change_date": change.get("change_date"),
                "old_meter_no": change.get("old_meter_no"),
                "new_meter_no": change.get("new_meter_no"),
                "reason": change.get("reason", ""),
                "module": "换表/回绕识别",
            },
            segments=[
                {
                    "meter_no": change.get("old_meter_no"),
                    "from": start.isoformat(),
                    "to": change.get("change_date"),
                    "days": old_days,
                    "start_reading": _t(before),
                    "end_reading": _t(old_final),
                    "usage": old_seg,
                },
                {
                    "meter_no": change.get("new_meter_no"),
                    "from": change.get("change_date"),
                    "to": end.isoformat(),
                    "days": new_days,
                    "start_reading": _t(new_initial),
                    "end_reading": _t(after),
                    "usage": new_seg,
                },
            ],
        )
        return base

    # b) 回绕
    prev_limit = full_scale * ROLLOVER_PREV_RATIO
    next_limit = full_scale * ROLLOVER_NEXT_RATIO
    if before >= prev_limit and after <= next_limit:
        usage = _t(full_scale - before + after)
        base.update(
            status="rollover",
            usage=usage,
            formula=(
                f"回绕（量程 {full_scale:g}）：{full_scale:g} − 前读数 {before:g} + 后读数 {after:g} = {usage:g}"
            ),
            reason="",
            rollover={"full_scale": _t(full_scale), "prev_value": _t(before), "next_value": _t(after)},
        )
        return base

    # c) 无法判定
    base.update(
        status="anomaly",
        usage=None,
        formula="",
        reason=(
            f"后读数 {after:g} < 前读数 {before:g}，且无换表记录、"
            f"前读数未接近量程顶部（{before:g} < {prev_limit:g}），无法判定回绕或换表，已隔离"
        ),
    )
    return base


def restore_series(
    meter_id: str,
    kind: str,
    points: list[dict],
    *,
    changes: list[dict],
    full_scale: float,
    max_gap_days: int,
) -> dict:
    """整条序列的区间用量还原。"""
    intervals: list[dict] = []
    anomalies: list[dict] = []
    rollovers: list[dict] = []
    meter_changes: list[dict] = []
    usage_total = 0.0

    for prev, nxt in zip(points, points[1:]):
        change = find_change(changes, meter_id, prev["read_at"], nxt["read_at"])
        interval = interval_usage(
            prev, nxt, meter_id=meter_id, kind=kind, full_scale=full_scale, change=change
        )
        interval["gap"] = interval["days"] > max_gap_days
        intervals.append(interval)
        if interval["usage"] is not None:
            usage_total += interval["usage"]
        if interval["status"] == "anomaly":
            anomalies.append(interval)
        elif interval["status"] == "rollover":
            rollovers.append(interval)
        elif interval["status"] == "meter_change":
            meter_changes.append(interval)

    return {
        "meter_id": meter_id,
        "kind": kind,
        "intervals": intervals,
        "anomalies": anomalies,
        "rollovers": rollovers,
        "meter_changes": meter_changes,
        "interval_count": len(intervals),
        "usage_total": round(usage_total, 2),
    }


def restore_all(
    series: dict[tuple[str, str], list[dict]],
    meters: list[dict],
    changes: list[dict],
    *,
    full_scale: float = DEFAULT_FULL_SCALE,
    max_gap_days: int = 31,
    meter_gaps: dict[str, list[dict]] | None = None,
) -> dict:
    """全部表 × 全部类型的还原结果 + 去重后的问题清单。"""
    scales = {
        str(m.get("meter_id")): float(m.get("full_scale") or full_scale) for m in meters or []
    }
    results: dict[str, dict] = {}
    anomaly_bag: dict[tuple[str, str], dict] = {}
    for (meter_id, kind), points in sorted(series.items()):
        scale = scales.get(meter_id, full_scale)
        restored = restore_series(
            meter_id,
            kind,
            points,
            changes=changes,
            full_scale=scale,
            max_gap_days=max_gap_days,
        )
        entry = results.setdefault(meter_id, {"meter_id": meter_id, "kinds": {}, "anomalies": [], "rollovers": [], "changes": []})
        entry["kinds"][kind] = restored
        for item in restored["rollovers"]:
            entry["rollovers"].append(item)
        for item in restored["meter_changes"]:
            entry["changes"].append(item)
        for item in restored["anomalies"]:
            key = (item["start"].isoformat(), item["end"].isoformat())
            bucket = anomaly_bag.setdefault(
                (meter_id, f"{key[0]}|{key[1]}"),
                {
                    "meter_id": meter_id,
                    "start": key[0],
                    "end": key[1],
                    "prev_value": item["prev_value"],
                    "next_value": item["next_value"],
                    "reason": item["reason"],
                    "kinds": [],
                },
            )
            bucket["kinds"].append(kind)

    for (_meter_id, _key), bucket in sorted(anomaly_bag.items()):
        results[bucket["meter_id"]]["anomalies"].append(bucket)
    for entry in results.values():
        entry["anomalies"].sort(key=lambda item: (item["start"], item["end"]))
        entry["gaps"] = (meter_gaps or {}).get(entry["meter_id"], [])
    return results
