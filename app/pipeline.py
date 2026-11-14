"""编排：读数序列 → 用量还原 → 月度分摊 → 分时/缺口校验 → 月度台账与复核清单。

一次 analyze 输出全部月份（数据只还原一次），指定 month 时额外产出该月的聚焦结果。
"""

from __future__ import annotations

from datetime import date

from . import proration, readings, restore, settings, summary, validation

KINDS = readings.KINDS


class PipelineError(Exception):
    def __init__(self, message: str, *, details: list[dict] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        self.status = status


def analyze(
    rows: list[dict],
    meters: list[dict],
    changes: list[dict],
    *,
    month: str | None = None,
    meter_ids: list[str] | None = None,
    full_scale: float = settings.DEFAULT_FULL_SCALE,
    max_gap_days: int = settings.MAX_GAP_DAYS,
    tou_tolerance: float = settings.TOU_TOLERANCE,
) -> dict:
    if not rows:
        raise PipelineError("读数库为空，请先导入读数 CSV 或样例数据")

    series_all = readings.group_series(rows)
    available = sorted({meter for (meter, _kind) in series_all})
    requested = [m for m in (meter_ids or []) if m]
    unknown = [m for m in requested if m not in available]
    if unknown:
        raise PipelineError(
            f"表号不存在：{', '.join(unknown)}",
            details=[{"field": "meter_ids", "message": f"可选表号：{', '.join(available)}"}],
        )
    selected = requested or available
    series = {key: value for key, value in series_all.items() if key[0] in selected}

    meter_index = {str(item.get("meter_id")): item for item in meters or []}
    gaps_by_meter = readings.meter_gaps(series, max_gap_days)
    restored = restore.restore_all(
        series,
        meters,
        changes,
        full_scale=full_scale,
        max_gap_days=max_gap_days,
        meter_gaps=gaps_by_meter,
    )

    # 逐表逐类型分摊到全部月份
    allocations: dict[tuple[str, str], dict] = {}
    for meter_id, entry in restored.items():
        for kind in KINDS:
            kind_result = entry["kinds"].get(kind)
            if not kind_result:
                continue
            allocations[(meter_id, kind)] = proration.allocate(kind_result["intervals"])

    months = sorted(
        {
            month_key
            for alloc in allocations.values()
            for month_key in alloc["by_month"]
        }
    )
    if not months:
        raise PipelineError("读数区间不足两个抄表点，无法还原任何月份用量")

    ledger = summary.build_ledger(
        months=months,
        restored=restored,
        allocations=allocations,
        meter_index=meter_index,
        gaps_by_meter=gaps_by_meter,
        selected=selected,
        tou_tolerance=tou_tolerance,
        full_scale=full_scale,
        max_gap_days=max_gap_days,
    )

    payload = {
        "months": ledger["months"],
        "month_order": months,
        "review": ledger["review"],
        "stats": ledger["stats"],
        "selected": selected,
        "available": available,
        "full_scale": round(float(full_scale), 2),
        "max_gap_days": max_gap_days,
        "tou_tolerance": tou_tolerance,
        "monitored": ledger["monitored"],
    }

    if month:
        try:
            month_start, month_end = proration.parse_month(month)
        except ValueError as exc:
            raise PipelineError(str(exc), details=[{"field": "month", "message": "例如 2026-03"}]) from exc
        focus = summary.build_focus(
            month=month,
            month_start=month_start,
            month_end=month_end,
            months=ledger["months"],
            restored=restored,
            allocations=allocations,
            gaps_by_meter=gaps_by_meter,
            meter_index=meter_index,
            selected=selected,
            review=ledger["review"],
            tou_tolerance=tou_tolerance,
            full_scale=full_scale,
            max_gap_days=max_gap_days,
        )
        if not focus["has_data"]:
            raise PipelineError(
                f"{month} 没有任何读数覆盖区间",
                details=[{"field": "month", "message": f"数据覆盖：{months[0]} ~ {months[-1]}"}],
            )
        payload["focus"] = focus
        payload["month"] = month
    return payload
