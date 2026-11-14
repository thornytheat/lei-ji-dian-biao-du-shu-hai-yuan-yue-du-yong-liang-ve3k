"""汇总：月度用能表（每表每月 total/peak/valley/缺口天数/异常数）、复核清单、CSV 导出。"""

from __future__ import annotations

import csv
import io
from datetime import date

from . import proration, readings, validation

KINDS = readings.KINDS
KIND_LABELS = readings.KIND_LABELS
STATUS_ORDER = {"anomaly": 0, "tou_mismatch": 1, "gap": 2, "ok": 3}
STATUS_LABELS = {
    "anomaly": "存在隔离异常",
    "tou_mismatch": "分时超差",
    "gap": "存在抄表缺口",
    "ok": "正常",
}
LEVEL_LABELS = {"P1": "P1 立即复核", "P2": "P2 本月内复核", "P3": "P3 确认留存"}


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _interval_overlaps_month(interval: dict, month_start: date, month_end: date) -> bool:
    return validation.overlaps(interval["start"], interval["end"], month_start, month_end)


def _evidence_row(interval: dict, meter_name: str, month: str) -> dict:
    share = None
    part_days = None
    for part in (interval.get("proration") or {}).get("parts", []):
        if part["month"] == month:
            share = part["share"]
            part_days = part["days"]
    return {
        "meter_id": interval["meter_id"],
        "meter_name": meter_name,
        "kind": interval["kind"],
        "kind_label": KIND_LABELS.get(interval["kind"], interval["kind"]),
        "start": interval["start"].isoformat(),
        "end": interval["end"].isoformat(),
        "days": interval["days"],
        "status": interval["status"],
        "status_label": {
            "normal": "正常做差",
            "meter_change": "换表分段",
            "rollover": "回绕还原",
            "anomaly": "隔离（不参与汇总）",
        }.get(interval["status"], interval["status"]),
        "prev_value": interval["prev_value"],
        "next_value": interval["next_value"],
        "usage": _round(interval.get("usage")),
        "formula": interval.get("formula") or "",
        "reason": interval.get("reason") or "",
        "gap": interval.get("gap", False),
        "month_share": share,
        "month_part_days": part_days,
        "proration_formula": (interval.get("proration") or {}).get("formula", ""),
        "segments": interval.get("segments") or [],
        "change": interval.get("change"),
        "rollover": interval.get("rollover"),
    }


def _gap_row(meter_id: str, meter_name: str, gap: dict, month: str, month_start: date, month_end: date) -> dict:
    return {
        "meter_id": meter_id,
        "meter_name": meter_name,
        "month": month,
        "start": gap["start"].isoformat() if isinstance(gap["start"], date) else str(gap["start"]),
        "end": gap["end"].isoformat() if isinstance(gap["end"], date) else str(gap["end"]),
        "span_days": gap["span_days"],
        "missing_days": gap["missing_days"],
        "month_days": validation.gap_days_in_month(gap, month_start, month_end),
        "suggested_read_at": gap["suggested_read_at"],
        "note": gap["note"],
        "prev_value": gap["prev_value"],
        "next_value": gap["next_value"],
    }


def build_ledger(
    *,
    months: list[str],
    restored: dict,
    allocations: dict,
    meter_index: dict,
    gaps_by_meter: dict,
    selected: list[str],
    tou_tolerance: float,
    full_scale: float,
    max_gap_days: int,
) -> dict:
    ledger_months: dict[str, dict] = {}
    review: list[dict] = []
    monitored: list[dict] = []
    usage_total = 0.0
    interval_total = 0
    anomaly_total = 0

    for meter_id in selected:
        entry = restored.get(meter_id, {})
        meta = meter_index.get(meter_id, {})
        monitored.append(
            {
                "meter_id": meter_id,
                "name": meta.get("name") or f"表 {meter_id}",
                "location": meta.get("location") or "未登记",
                "full_scale": _round(meta.get("full_scale")) or _round(full_scale),
                "interval_count": sum(
                    len(entry.get("kinds", {}).get(kind, {}).get("intervals", [])) for kind in KINDS
                ),
            }
        )

    for month in months:
        month_start, month_end = proration.parse_month(month)
        month_block = {
            "month": month,
            "rows": [],
            "evidence": [],
            "anomalies": [],
            "gaps": [],
            "changes": [],
            "rollovers": [],
            "checks": [],
            "review": [],
        }
        for meter_id in selected:
            meta = meter_index.get(meter_id, {})
            meter_name = meta.get("name") or f"表 {meter_id}"
            values = {}
            for kind in KINDS:
                alloc = allocations.get((meter_id, kind), {"by_month": {}})
                values[kind] = _round(alloc["by_month"].get(month))
            tou = validation.time_of_use_check(values["total"], values["peak"], values["valley"], tou_tolerance)

            gaps = gaps_by_meter.get(meter_id, [])
            gap_rows = [
                _gap_row(meter_id, meter_name, gap, month, month_start, month_end)
                for gap in gaps
                if validation.gap_days_in_month(gap, month_start, month_end) > 0
            ]
            gap_days = sum(row["month_days"] for row in gap_rows)

            anomalies = [
                item
                for item in restored.get(meter_id, {}).get("anomalies", [])
                if validation.overlaps(
                    date.fromisoformat(item["start"]), date.fromisoformat(item["end"]), month_start, month_end
                )
            ]
            changes = [
                item
                for item in restored.get(meter_id, {}).get("changes", [])
                if validation.overlaps(item["start"], item["end"], month_start, month_end)
            ]
            rollovers = [
                item
                for item in restored.get(meter_id, {}).get("rollovers", [])
                if validation.overlaps(item["start"], item["end"], month_start, month_end)
            ]

            isolated_count = len(anomalies)
            tou_count = 0 if tou["ok"] else 1
            if values["total"] is None and values["peak"] is None and values["valley"] is None and not isolated_count and not gap_days:
                continue  # 本月该表无任何读数覆盖
            anomaly_count = isolated_count + tou_count
            if isolated_count:
                status = "anomaly"
            elif tou_count:
                status = "tou_mismatch"
            elif gap_days:
                status = "gap"
            else:
                status = "ok"

            row = {
                "meter_id": meter_id,
                "meter_name": meter_name,
                "month": month,
                "total": values["total"],
                "peak": values["peak"],
                "valley": values["valley"],
                "tou": tou,
                "gap_days": gap_days,
                "gap_count": len(gap_rows),
                "isolated_count": isolated_count,
                "tou_count": tou_count,
                "anomaly_count": anomaly_count,
                "status": status,
                "status_label": STATUS_LABELS[status],
            }
            month_block["rows"].append(row)
            month_block["gaps"].extend(gap_rows)
            month_block["anomalies"].extend(
                [
                    {
                        "meter_id": meter_id,
                        "meter_name": meter_name,
                        "start": item["start"],
                        "end": item["end"],
                        "prev_value": item["prev_value"],
                        "next_value": item["next_value"],
                        "reason": item["reason"],
                        "kinds": item["kinds"],
                    }
                    for item in anomalies
                ]
            )
            month_block["changes"].extend(
                [
                    {
                        "meter_id": meter_id,
                        "meter_name": meter_name,
                        "start": item["start"].isoformat(),
                        "end": item["end"].isoformat(),
                        "change": item.get("change"),
                        "segments": item.get("segments"),
                        "usage": _round(item.get("usage")),
                        "formula": item.get("formula"),
                    }
                    for item in changes
                ]
            )
            month_block["rollovers"].extend(
                [
                    {
                        "meter_id": meter_id,
                        "meter_name": meter_name,
                        "start": item["start"].isoformat(),
                        "end": item["end"].isoformat(),
                        "usage": _round(item.get("usage")),
                        "formula": item.get("formula"),
                        "rollover": item.get("rollover"),
                    }
                    for item in rollovers
                ]
            )
            if not tou["ok"] and tou["status"] == "mismatch":
                month_block["checks"].append({"meter_id": meter_id, "meter_name": meter_name, "tou": tou})

            usage_total += values["total"] or 0.0
            anomaly_total += isolated_count
            interval_total += sum(
                1
                for kind in KINDS
                for interval in restored.get(meter_id, {}).get("kinds", {}).get(kind, {}).get("intervals", [])
                if _interval_overlaps_month(interval, month_start, month_end)
            )

        # 本月逐条依据
        for meter_id in selected:
            meta = meter_index.get(meter_id, {})
            meter_name = meta.get("name") or f"表 {meter_id}"
            for kind in KINDS:
                for interval in restored.get(meter_id, {}).get("kinds", {}).get(kind, {}).get("intervals", []):
                    if not _interval_overlaps_month(interval, month_start, month_end):
                        continue
                    month_block["evidence"].append(_evidence_row(interval, meter_name, month))

        month_block["review"] = _review_items(month_block, month)
        review.extend(month_block["review"])
        ledger_months[month] = month_block

    stats = {
        "months": len(months),
        "meters": len(selected),
        "intervals": interval_total,
        "usage_total": round(usage_total, 2),
        "review_items": len(review),
        "review_p1": sum(1 for item in review if item["level"] == "P1"),
        "review_p2": sum(1 for item in review if item["level"] == "P2"),
        "review_p3": sum(1 for item in review if item["level"] == "P3"),
        "isolated_anomalies": anomaly_total,
        "gaps": sum(len(block["gaps"]) for block in ledger_months.values()),
        "meter_changes": sum(len(block["changes"]) for block in ledger_months.values()),
        "rollovers": sum(len(block["rollovers"]) for block in ledger_months.values()),
    }
    return {"months": ledger_months, "review": review, "stats": stats, "monitored": monitored}


def _review_items(block: dict, month: str) -> list[dict]:
    items: list[dict] = []
    for check in block["checks"]:
        tou = check["tou"]
        items.append(
            {
                "month": month,
                "meter_id": check["meter_id"],
                "meter_name": check["meter_name"],
                "level": "P1",
                "level_label": LEVEL_LABELS["P1"],
                "kind": "分时超差",
                "summary": (
                    f"峰 {tou['peak']:g} + 谷 {tou['valley']:g} = {tou['sum']:g}，"
                    f"总量 {tou['total']:g}，偏差 {tou['diff']:+g} kWh"
                    f"（{tou['deviation_ratio'] * 100:+.2f}%，容差 ±{tou['allowed']:g}）"
                ),
                "action": "核对峰段寄存器是否冻结/时钟电池，按现场表码复核后做退补调整",
                "basis": "月度分摊后 total 与 peak+valley 比对",
            }
        )
    for item in block["anomalies"]:
        items.append(
            {
                "month": month,
                "meter_id": item["meter_id"],
                "meter_name": item["meter_name"],
                "level": "P1",
                "level_label": LEVEL_LABELS["P1"],
                "kind": "读数回退无法判定",
                "summary": f"{item['start']} → {item['end']} 读数 {item['prev_value']:g} → {item['next_value']:g}，"
                f"涉及 {'/'.join(KIND_LABELS.get(k, k) for k in item['kinds'])}",
                "action": "现场核表：确认是否换表/回绕漏登记，补齐换表记录或表计故障单后重算；该区间用量不参与汇总",
                "basis": item["reason"],
            }
        )
    for gap in block["gaps"]:
        items.append(
            {
                "month": month,
                "meter_id": gap["meter_id"],
                "meter_name": gap["meter_name"],
                "level": "P2",
                "level_label": LEVEL_LABELS["P2"],
                "kind": "抄表缺口",
                "summary": f"{gap['start']} → {gap['end']} 间隔 {gap['span_days']} 天，缺失 {gap['missing_days']} 天"
                f"（本月 {gap['month_days']} 天）",
                "action": f"建议抄表日 {gap['suggested_read_at']} 前后补抄一次，并把抄表周期压到 ≤31 天；缺口不插值",
                "basis": gap["note"],
            }
        )
    for change in block["changes"]:
        info = change.get("change") or {}
        items.append(
            {
                "month": month,
                "meter_id": change["meter_id"],
                "meter_name": change["meter_name"],
                "level": "P3",
                "level_label": LEVEL_LABELS["P3"],
                "kind": "换表分段确认",
                "summary": f"{info.get('change_date', change['start'])} 换表 "
                f"{info.get('old_meter_no', '旧表')} → {info.get('new_meter_no', '新表')}",
                "action": f"确认拆表读数与装表读数已签认后归档；本区间用量 {change['usage']:g} kWh",
                "basis": change.get("formula") or "",
            }
        )
    for rollover in block["rollovers"]:
        scale = (rollover.get("rollover") or {}).get("full_scale")
        items.append(
            {
                "month": month,
                "meter_id": rollover["meter_id"],
                "meter_name": rollover["meter_name"],
                "level": "P3",
                "level_label": LEVEL_LABELS["P3"],
                "kind": "回绕确认",
                "summary": f"{rollover['start']} → {rollover['end']} 满量程回绕（量程 {scale:g}）",
                "action": "确认表计满位回绕属实，不要把回绕当异常剔除；本区间用量已按量程还原",
                "basis": rollover.get("formula") or "",
            }
        )
    items.sort(key=lambda item: (item["level"], item["meter_id"], item["kind"]))
    return items


def build_focus(
    *,
    month: str,
    month_start: date,
    month_end: date,
    months: dict,
    restored: dict,
    allocations: dict,
    gaps_by_meter: dict,
    meter_index: dict,
    selected: list[str],
    review: list[dict],
    tou_tolerance: float,
    full_scale: float,
    max_gap_days: int,
) -> dict:
    block = months.get(month)
    if block is None:
        return {
            "month": month,
            "has_data": False,
            "rows": [],
            "evidence": [],
            "anomalies": [],
            "gaps": [],
            "changes": [],
            "rollovers": [],
            "checks": [],
            "review": [],
            "totals": {},
        }

    rows = block["rows"]
    evidence = block["evidence"]
    by_meter: list[dict] = []
    for row in rows:
        meter_evidence = [item for item in evidence if item["meter_id"] == row["meter_id"]]
        by_meter.append(
            {
                "row": row,
                "evidence": meter_evidence,
                "total_evidence": [item for item in meter_evidence if item["kind"] == "total"],
                "peak_evidence": [item for item in meter_evidence if item["kind"] == "peak"],
                "valley_evidence": [item for item in meter_evidence if item["kind"] == "valley"],
            }
        )

    usage_total = round(sum((row["total"] or 0.0) for row in rows), 2)
    peak_total = round(sum((row["peak"] or 0.0) for row in rows), 2)
    valley_total = round(sum((row["valley"] or 0.0) for row in rows), 2)
    month_review = [item for item in review if item["month"] == month]
    return {
        "month": month,
        "has_data": True,
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "rows": rows,
        "by_meter": by_meter,
        "evidence": evidence,
        "anomalies": block["anomalies"],
        "gaps": block["gaps"],
        "changes": block["changes"],
        "rollovers": block["rollovers"],
        "checks": block["checks"],
        "review": month_review,
        "totals": {
            "usage_total": usage_total,
            "peak_total": peak_total,
            "valley_total": valley_total,
            "meters": len(rows),
            "intervals": len(evidence),
            "anomalies": sum(row["isolated_count"] for row in rows),
            "tou_flagged": sum(row["tou_count"] for row in rows),
            "anomaly_count": sum(row["anomaly_count"] for row in rows),
            "gap_segments": len(block["gaps"]),
            "gap_days": sum(row["gap_days"] for row in rows),
            "changes": len(block["changes"]),
            "rollovers": len(block["rollovers"]),
            "review_items": len(month_review),
        },
        "full_scale": round(float(full_scale), 2),
        "max_gap_days": max_gap_days,
    }


def ledger_csv(months: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "月份",
            "表号",
            "表名",
            "总电量kWh",
            "峰段kWh",
            "谷段kWh",
            "峰谷合计",
            "偏差kWh",
            "偏差率",
            "分时校验",
            "缺口天数",
            "异常数",
            "隔离异常数",
            "状态",
        ]
    )
    for month in sorted(months):
        for row in months[month]["rows"]:
            writer.writerow(
                [
                    month,
                    row["meter_id"],
                    row["meter_name"],
                    _csv_num(row["total"]),
                    _csv_num(row["peak"]),
                    _csv_num(row["valley"]),
                    _csv_num(row["tou"]["sum"]),
                    _csv_num(row["tou"]["diff"]),
                    "" if row["tou"]["deviation_ratio"] is None else f"{row['tou']['deviation_ratio'] * 100:.2f}%",
                    "通过" if row["tou"]["ok"] else ("超差" if row["tou"]["status"] == "mismatch" else "缺数据"),
                    row["gap_days"],
                    row["anomaly_count"],
                    row["isolated_count"],
                    row["status_label"],
                ]
            )
    return buffer.getvalue()


def review_csv(items: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["月份", "表号", "表名", "级别", "类型", "问题说明", "建议动作", "依据"])
    for item in items:
        writer.writerow(
            [
                item["month"],
                item["meter_id"],
                item["meter_name"],
                item["level_label"],
                item["kind"],
                item["summary"],
                item["action"].replace("；", "；"),
                item["basis"],
            ]
        )
    return buffer.getvalue()


def _csv_num(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"
