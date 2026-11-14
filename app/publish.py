"""补录更正发布：整批校验 → 只重算受影响月份 → 一次性切换版本。

对外可见的批次永远只有「当前发布版本」。旧版本以快照保留在 run_versions 里，
发布中途失败时 runs.current_version_id 不变，页面/下载/导出继续读旧版本；
同一批更正重试按 request_hash 命中已有批次，不会新增批次或版本。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from . import corrections, pipeline, store, summary


class CorrectionError(Exception):
    def __init__(self, message: str, *, status: int = 400, details: list[dict] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or []


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def fingerprint(rows: list[dict]) -> str:
    """读数集合指纹：判断批次快照是否仍对应当前读数。"""
    digest = hashlib.sha256()
    ordered = sorted(
        (
            row["meter_id"],
            row["read_at"].isoformat(),
            row["kind"],
            f"{float(row['reading_kwh']):.1f}",
        )
        for row in rows
    )
    for meter_id, read_at, kind, value in ordered:
        digest.update(f"{meter_id}|{read_at}|{kind}|{value}\n".encode("utf-8"))
    return digest.hexdigest()


def _merge_review(
    old_review: list[dict],
    new_review: list[dict],
    month_order: list[str],
    affected: set[str],
) -> list[dict]:
    """按月份顺序拼接复核清单：受影响月份用新结果，其余沿用旧版本。"""
    buckets: dict[str, list[dict]] = {}
    for item in old_review:
        if item.get("month") not in affected:
            buckets.setdefault(item.get("month", ""), []).append(item)
    for item in new_review:
        buckets.setdefault(item.get("month", ""), []).append(item)
    merged: list[dict] = []
    for month in month_order:
        merged.extend(buckets.pop(month, []))
    for month in sorted(buckets):
        merged.extend(buckets[month])
    return merged


def rebuild_payload(
    run: dict,
    candidate_rows: list[dict],
    meters: list[dict],
    changes: list[dict],
    affected: list[str],
) -> dict:
    """只重算受影响月份，再把新结果合并回旧版本的其他月份。"""
    affected_set = set(affected)
    month = run.get("month")
    focus_month = month if month in affected_set else None
    result = pipeline.analyze(
        candidate_rows,
        meters,
        changes,
        month=focus_month,
        meter_ids=run.get("selected") or None,
        full_scale=float(run.get("full_scale") or 99999.9),
        max_gap_days=int(run.get("max_gap_days") or 31),
        tou_tolerance=float(run.get("tou_tolerance") or 0.005),
        only_months=affected,
    )

    merged_ledger = {
        key: value for key, value in (run.get("ledger") or {}).items() if key not in affected_set
    }
    merged_ledger.update({key: block for key, block in result["months"].items()})
    month_order = sorted(merged_ledger)
    merged_review = _merge_review(
        run.get("review") or [], result.get("review") or [], month_order, affected_set
    )

    payload = dict(run)
    payload.update(
        {
            "selected": result["selected"],
            "available": result["available"],
            "full_scale": result["full_scale"],
            "max_gap_days": result["max_gap_days"],
            "tou_tolerance": result["tou_tolerance"],
            "ledger": merged_ledger,
            "month_order": month_order,
            "review": merged_review,
            "stats": summary.recompute_stats(merged_ledger, merged_review, len(result["selected"])),
            "monitored": result["monitored"],
            "data_version": fingerprint(candidate_rows),
            "affected_months": affected,
            "recomputed_months": sorted(result["months"]),
        }
    )
    if focus_month is not None:
        payload["focus"] = result["focus"]
        payload["focus_review"] = result["focus"]["review"]
        payload["month_start"] = result["focus"]["month_start"]
        payload["month_end"] = result["focus"]["month_end"]
    else:
        # 聚焦月份不在受影响范围时沿用旧快照，复核清单同步取旧版对应月份。
        payload["focus"] = run.get("focus")
        payload["focus_review"] = run.get("focus_review") or [
            item for item in (run.get("review") or []) if item.get("month") == month
        ]
    return payload


def _result(
    *,
    correction_id: str,
    run: dict,
    version_id: str,
    revision: int,
    affected: list[str],
    computed: list[str] | None = None,
    unchanged: list[str] | None = None,
    changed: int = 0,
    idempotent: bool = False,
) -> dict:
    focus = run.get("focus") or {}
    return {
        "ok": True,
        "correction_id": correction_id,
        "run_id": run["run_id"],
        "month": run.get("month"),
        "version_id": version_id,
        "revision": revision,
        "affected_months": affected,
        "recomputed_months": computed or [],
        "unchanged_months": unchanged or [],
        "changed_readings": changed,
        "idempotent": idempotent,
        "totals": focus.get("totals") or {},
        "result_url": f"/result/{run['run_id']}",
        "export_url": f"/api/runs/{run['run_id']}/ledger.csv",
    }


def _plan(run: dict, rows: list[dict], known_meters: set[str], normalized: list[dict]) -> dict:
    unknown = sorted({row["meter_id"] for row in normalized if row["meter_id"] not in known_meters})
    if unknown:
        raise CorrectionError(
            f"表号不存在：{', '.join(unknown)}",
            details=[{"field": "meter_id", "message": f"可选表号：{', '.join(sorted(known_meters))}"}],
        )
    selected = set(run.get("selected") or [])
    outside = sorted({row["meter_id"] for row in normalized if selected and row["meter_id"] not in selected})
    if outside:
        raise CorrectionError(
            f"表号不在该批次范围内：{', '.join(outside)}",
            details=[
                {
                    "field": "meter_id",
                    "message": f"该批次只包含：{', '.join(sorted(selected))}；"
                    f"如需补录其它表，请重新执行还原生成包含该表的批次",
                }
            ],
        )
    affected = corrections.affected_months(normalized, rows)
    month_order = run.get("month_order") or sorted((run.get("ledger") or {}).keys())
    affected_set = set(affected)
    return {
        "affected": affected,
        "recomputed": [month for month in affected if month in set(month_order)],
        "unchanged": [month for month in month_order if month not in affected_set],
        "request_hash": corrections.request_hash(run["run_id"], normalized),
    }


def apply_correction(run_id: str, raw_rows) -> dict:
    """校验并发布一整批补录更正。"""
    # 1) 完整校验：任一条不合法，整批拒绝，读数和批次都不动。
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
        if not run:
            raise CorrectionError(f"还原批次 {run_id} 不存在", status=404)
        known_meters = {meter["meter_id"] for meter in store.all_meters(conn)}

    normalized, problems = corrections.normalize(raw_rows, known_meters=known_meters)
    if problems:
        raise CorrectionError(f"更正有 {len(problems)} 处错误，整批未应用", details=problems)

    # 2) 幂等登记：同一 (批次, 内容) 只对应一条更正记录。
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
        if not run:
            raise CorrectionError(f"还原批次 {run_id} 不存在", status=404)
        rows = store.all_readings(conn)
        snapshot = run.get("data_version")
        if snapshot and snapshot != store.readings_fingerprint(conn):
            raise CorrectionError(
                "批次快照与当前读数不一致（读数已被整份导入替换），"
                "请先重新执行还原生成新批次再补录",
                status=409,
            )
        plan = _plan(run, rows, known_meters, normalized)
        existing = store.find_correction_by_hash(conn, run_id, plan["request_hash"])
        if existing and existing["status"] == "applied":
            current_rows, pending = corrections.apply_to_rows(
                store.all_readings(conn), normalized
            )
            if pending == 0:
                info = store.version_info(conn, existing.get("version_id") or "")
                return _result(
                    correction_id=existing["correction_id"],
                    run=run,
                    version_id=existing.get("version_id") or run.get("version_id", ""),
                    revision=info["seq"] if info else int(run.get("revision") or 1),
                    affected=existing.get("affected_months") or plan["affected"],
                    computed=plan["recomputed"],
                    unchanged=plan["unchanged"],
                    idempotent=True,
                )
            # 该批曾发布过，但读数后来被别的更正覆盖：按新内容重新发布，
            # 复用同一条更正记录，不新增批次。
            correction_id = existing["correction_id"]
            store.update_correction(conn, correction_id, status="pending", error="")
        elif existing:
            correction_id = existing["correction_id"]
            store.update_correction(conn, correction_id, status="pending", error="")
        else:
            correction_id = f"corr-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            store.save_correction(
                conn,
                correction_id=correction_id,
                run_id=run_id,
                status="pending",
                request_hash=plan["request_hash"],
                payload={"readings": corrections.serialize(normalized)},
                affected_months=plan["affected"],
                now=now_iso(),
            )
        conn.commit()

    # 3) 计算完整新版本后再一次性切换；期间任何失败都不影响已发布版本。
    try:
        with store.session() as conn:
            store.init_schema(conn)
            run = store.load_run(conn, run_id)
            if not run:
                raise CorrectionError(f"还原批次 {run_id} 不存在", status=404)
            rows = store.all_readings(conn)
            candidate_rows, changed = corrections.apply_to_rows(rows, normalized)

            if changed == 0:
                store.update_correction(
                    conn,
                    correction_id,
                    status="applied",
                    applied_at=now_iso(),
                    version_id=run.get("version_id", ""),
                )
                store.add_event(
                    conn,
                    "correction_skip",
                    f"补录批次 {correction_id} 与当前读数一致，未生成新版本",
                    {"run_id": run_id, "correction_id": correction_id},
                    now_iso(),
                )
                conn.commit()
                return _result(
                    correction_id=correction_id,
                    run=run,
                    version_id=run.get("version_id", ""),
                    revision=int(run.get("revision") or 1),
                    affected=plan["affected"],
                    computed=plan["recomputed"],
                    unchanged=plan["unchanged"],
                    idempotent=True,
                )

            new_payload = rebuild_payload(
                run, candidate_rows, store.all_meters(conn), store.all_changes(conn), plan["affected"]
            )
            new_payload["correction_id"] = correction_id

            # 同一事务：读数落库 + 写新版本 + 指针切换 + 批次状态 + 当前发布批次。
            store.apply_reading_corrections(conn, normalized, now_iso())
            published = store.publish_version(
                conn,
                run_id=run_id,
                payload=new_payload,
                correction_id=correction_id,
                affected_months=plan["affected"],
                now=now_iso(),
            )
            store.update_correction(
                conn,
                correction_id,
                status="applied",
                applied_at=now_iso(),
                version_id=published["version_id"],
            )
            store.set_meta(conn, "current_run_id", run_id)
            store.add_event(
                conn,
                "correction",
                f"补录更正 {correction_id}：重算 {', '.join(plan['affected']) or '无'}，"
                f"版本 {published['version_id']}",
                {
                    "run_id": run_id,
                    "correction_id": correction_id,
                    "version_id": published["version_id"],
                    "affected_months": plan["affected"],
                    "changed_readings": changed,
                },
                now_iso(),
            )
            conn.commit()
            updated = store.load_run(conn, run_id) or run
        return _result(
            correction_id=correction_id,
            run=updated,
            version_id=published["version_id"],
            revision=published["seq"],
            affected=plan["affected"],
            computed=new_payload.get("recomputed_months") or plan["recomputed"],
            unchanged=plan["unchanged"],
            changed=changed,
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败都保留旧版本并记录原因
        _record_failure(run_id, correction_id, exc)
        if isinstance(exc, CorrectionError):
            raise
        raise CorrectionError(f"更正发布失败，旧版本保持不变：{exc}", status=500) from exc


def _record_failure(run_id: str, correction_id: str, exc: Exception) -> None:
    try:
        with store.session() as conn:
            store.init_schema(conn)
            store.update_correction(conn, correction_id, status="failed", error=str(exc))
            store.add_event(
                conn,
                "correction_failed",
                f"补录更正失败，旧版本继续对外发布：{exc}",
                {"run_id": run_id, "correction_id": correction_id},
                now_iso(),
            )
            conn.commit()
    except Exception:  # pragma: no cover - 记录失败不能再掩盖原始错误
        pass


def published_view(conn) -> dict | None:
    """当前对外发布的全期结果。

    只有批次快照与当前读数一致时才返回快照，否则返回 None，由调用方回退实时计算
    （例如导入新读数后尚未重新执行还原）。
    """
    current = store.get_meta(conn, "current_run_id")
    run = store.load_run(conn, current) if current else None
    if not run:
        run = store.latest_run(conn)
    if not run or not run.get("data_version"):
        return None
    if run["data_version"] != store.readings_fingerprint(conn):
        return None
    return {
        "run": run,
        "months": run.get("ledger") or {},
        "month_order": run.get("month_order") or [],
        "review": run.get("review") or [],
        "stats": run.get("stats") or {},
        "monitored": run.get("monitored") or [],
    }
