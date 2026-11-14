"""FastAPI 路由（薄）：页面渲染 + JSON API。规则全在 readings/restore/proration/validation/summary。"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import pipeline, proration, readings, seed, settings, store, summary

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _fmt(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["num"] = lambda value: _fmt(value, 2)
templates.env.filters["num1"] = lambda value: _fmt(value, 1)
templates.env.filters["pct"] = lambda value: "—" if value is None else f"{float(value) * 100:+.2f}%"
templates.env.filters["signed"] = lambda value: "—" if value is None else f"{float(value):+,.2f}"

_CACHE: dict[str, Any] = {"key": None, "value": None}


@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
    yield


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def load_data() -> tuple[list[dict], list[dict], list[dict]]:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.all_readings(conn)
        meters = store.all_meters(conn)
        changes = store.all_changes(conn)
    return rows, meters, changes


def cache_key(rows: list[dict], meters: list[dict], changes: list[dict]) -> str:
    span = f"{rows[0]['read_at']}~{rows[-1]['read_at']}" if rows else "empty"
    return f"{len(rows)}|{len(meters)}|{len(changes)}|{span}|{settings.SEED_VERSION}"


def analyze_all(force: bool = False) -> dict:
    """全量还原一次并缓存（导入后 force=True 失效缓存）。"""
    rows, meters, changes = load_data()
    key = cache_key(rows, meters, changes)
    if not force and _CACHE["key"] == key and _CACHE["value"] is not None:
        return _CACHE["value"]
    result = pipeline.analyze(rows, meters, changes)
    _CACHE["key"] = key
    _CACHE["value"] = result
    return result


def base_context(request: Request, **extra: Any) -> dict:
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "page": "",
        "active": "",
        "error": None,
        "notice": None,
        "seed_result": getattr(app.state, "seed_result", None),
        "full_scale": settings.DEFAULT_FULL_SCALE,
        "max_gap_days": settings.MAX_GAP_DAYS,
        "tou_tolerance": settings.TOU_TOLERANCE,
    }
    context.update(extra)
    return context


def pipeline_error(exc: pipeline.PipelineError) -> HTTPException:
    detail = exc.message
    if exc.details:
        detail = f"{detail}（{exc.details[0]['message']}）"
    return HTTPException(status_code=exc.status, detail=detail)


# --------------------------------------------------------------------------- 请求模型


class RestoreRequest(BaseModel):
    month: str = Field(..., examples=["2026-03"])
    meter_ids: list[str] = Field(default_factory=list)
    full_scale: float | None = None
    max_gap_days: int | None = None


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        readings_count = store.count_readings(conn)
        meters_count = len(store.all_meters(conn))
        changes_count = store.count_changes(conn)
        span = store.reading_span(conn)
    return {
        "status": "ok",
        "app": settings.APP_SHORT,
        "readings": readings_count,
        "meters": meters_count,
        "changes": changes_count,
        "readings_from": span["date_from"],
        "readings_to": span["date_to"],
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    stats = ledger["stats"] if ledger else {}
    months = ledger["month_order"] if ledger else []
    with store.session() as conn:
        runs = store.list_runs(conn, limit=6)
    context = base_context(
        request,
        page="index",
        active="index",
        runs=runs,
        latest_run=runs[0] if runs else None,
        error=error,
        stats=stats,
        months=months,
        month_count=len(months),
        monitored=ledger["monitored"] if ledger else [],
        latest_month=months[-1] if months else "",
        incidents=[
            {"module": "换表/回绕识别", "count": stats.get("meter_changes", 0), "unit": "次换表分段",
             "detail": "旧表拆表读数 + 新表装表读数两段相加"},
            {"module": "换表/回绕识别", "count": stats.get("rollovers", 0), "unit": "次满量程回绕",
             "detail": "量程 − 前读数 + 后读数"},
            {"module": "反复核清单", "count": stats.get("gaps", 0), "unit": "段抄表缺口",
             "detail": "间隔 > 31 天，只报区间与建议抄表日，不插值"},
            {"module": "用量还原", "count": stats.get("isolated_anomalies", 0), "unit": "条无法判定异常",
             "detail": "读数回退且无换表记录 → 隔离，不参与汇总"},
            {"module": "分时校验", "count": stats.get("review_p1", 0), "unit": "条 P1 复核项",
             "detail": "峰+谷 与总量偏差 > 0.5% 即记异常"},
        ],
        readings_rows=len(rows),
    )
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    months = ledger["month_order"] if ledger else []
    with store.session() as conn:
        runs = store.list_runs(conn, limit=8)
        events = store.list_events(conn, limit=12)
    context = base_context(
        request,
        page="workbench",
        active="workbench",
        error=error,
        meters=meters,
        readings_rows=len(rows),
        changes_count=len(changes),
        months=months,
        default_month=months[-1] if months else "2026-03",
        runs=runs,
        events=events,
        ledger_stats=ledger["stats"] if ledger else {},
        monitored=ledger["monitored"] if ledger else [],
    )
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result_page(request: Request, run_id: str) -> HTMLResponse:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    context = base_context(
        request,
        page="result",
        active="result",
        run=run,
        run_id=run_id,
        error=None if run else f"还原批次 {run_id} 不存在，请回到工作台重新执行",
    )
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, month: str | None = None) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    months = ledger["month_order"] if ledger else []
    focus_month = month if month in months else (months[-1] if months else "")
    with store.session() as conn:
        runs = store.list_runs(conn, limit=10)
        events = store.list_events(conn, limit=20)
    context = base_context(
        request,
        page="review",
        active="review",
        error=error,
        ledger=ledger,
        months=months,
        focus_month=focus_month,
        review_items=ledger["review"] if ledger else [],
        runs=runs,
        events=events,
        readings_rows=len(rows),
        changes_count=len(changes),
    )
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 导入


@app.post("/api/import/sample")
def api_import_sample() -> JSONResponse:
    with store.session() as conn:
        store.init_schema(conn)
        sample = seed.load_sample_files()
        result = seed.seed(conn, sample=sample, force=True)
        counts = {
            "readings": store.count_readings(conn),
            "meters": len(store.all_meters(conn)),
            "changes": store.count_changes(conn),
        }
    analyze_all(force=True)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "seeded": result["seeded"],
            "reason": result["reason"],
            "stats": sample["stats"],
            "counts": counts,
            "source": settings.SEED_SOURCE,
        },
    )


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)) -> JSONResponse:
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="读数文件编码无法识别，请另存为 UTF-8 或 GBK") from exc
    try:
        parsed = readings.parse_csv(text)
    except readings.ReadingError as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": exc.message, "problems": exc.problems, "filename": file.filename},
        )

    now = now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        replaced = store.replace_readings(conn, parsed["rows"], now)
        known = {meter["meter_id"] for meter in store.all_meters(conn)}
        new_meters = [
            {
                "meter_id": meter_id,
                "name": f"表 {meter_id}",
                "location": "导入登记",
                "full_scale": settings.DEFAULT_FULL_SCALE,
                "ct_ratio": 1.0,
            }
            for meter_id in parsed["stats"]["meters"]
            if meter_id not in known
        ]
        if new_meters:
            store.upsert_meters(conn, new_meters)
        store.add_event(
            conn,
            "import",
            f"导入读数 {replaced} 行（{file.filename or '上传文件'}）",
            parsed["stats"],
            now,
        )
    analyze_all(force=True)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "filename": file.filename,
            "replaced_rows": replaced,
            "stats": parsed["stats"],
            "new_meters": [item["meter_id"] for item in new_meters],
        },
    )


# --------------------------------------------------------------------------- 还原


@app.post("/api/restore")
def api_restore(payload: RestoreRequest = Body(...)) -> JSONResponse:
    try:
        proration.parse_month(payload.month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows, meters, changes = load_data()
    if not rows:
        raise HTTPException(status_code=400, detail="读数库为空，请先导入读数 CSV 或 /api/import/sample")
    full_scale = payload.full_scale or settings.DEFAULT_FULL_SCALE
    if full_scale <= 0:
        raise HTTPException(status_code=400, detail=f"量程必须为正数，实际 {full_scale}")
    max_gap_days = payload.max_gap_days or settings.MAX_GAP_DAYS
    if max_gap_days < 2:
        raise HTTPException(status_code=400, detail="缺口阈值至少 2 天")

    try:
        result = pipeline.analyze(
            rows,
            meters,
            changes,
            month=payload.month,
            meter_ids=payload.meter_ids,
            full_scale=full_scale,
            max_gap_days=max_gap_days,
        )
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc

    run_id = f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    created_at = now_iso()
    focus = result["focus"]
    run_payload = {
        "run_id": run_id,
        "created_at": created_at,
        "month": payload.month,
        "month_start": focus["month_start"],
        "month_end": focus["month_end"],
        "selected": result["selected"],
        "available": result["available"],
        "full_scale": result["full_scale"],
        "max_gap_days": result["max_gap_days"],
        "tou_tolerance": result["tou_tolerance"],
        "focus": focus,
        "review": focus["review"],
        "ledger": {month: result["months"][month]["rows"] for month in result["month_order"]},
        "month_order": result["month_order"],
        "stats": result["stats"],
        "monitored": result["monitored"],
    }
    with store.session() as conn:
        store.init_schema(conn)
        store.save_run(conn, run_payload)
        store.add_event(
            conn,
            "restore",
            f"还原 {payload.month}：{len(focus['rows'])} 块表，总用量 {focus['totals']['usage_total']:g} kWh",
            {
                "run_id": run_id,
                "month": payload.month,
                "meters": result["selected"],
                "totals": focus["totals"],
            },
            created_at,
        )

    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "run_id": run_id,
            "month": payload.month,
            "meters": result["selected"],
            "totals": focus["totals"],
            "rows": [
                {
                    "meter_id": row["meter_id"],
                    "meter_name": row["meter_name"],
                    "total": row["total"],
                    "peak": row["peak"],
                    "valley": row["valley"],
                    "gap_days": row["gap_days"],
                    "anomaly_count": row["anomaly_count"],
                    "status": row["status"],
                    "status_label": row["status_label"],
                }
                for row in focus["rows"]
            ],
            "anomalies": focus["anomalies"],
            "gaps": focus["gaps"],
            "changes": [
                {
                    "meter_id": item["meter_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "usage": item["usage"],
                    "formula": item["formula"],
                }
                for item in focus["changes"]
            ],
            "rollovers": [
                {
                    "meter_id": item["meter_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "usage": item["usage"],
                    "formula": item["formula"],
                }
                for item in focus["rollovers"]
            ],
            "review": focus["review"],
            "result_url": f"/result/{run_id}",
            "export_url": f"/api/runs/{run_id}/ledger.csv",
        },
    )


@app.get("/api/runs")
def api_runs() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        runs = store.list_runs(conn, limit=30)
    return {"ok": True, "runs": runs}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str) -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    return {"ok": True, "run": run}


# --------------------------------------------------------------------------- 导出


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs/{run_id}/ledger.csv")
def api_run_ledger(run_id: str) -> Response:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    month = run["month"]
    return _csv_response(
        summary.ledger_csv({month: run["focus"]}),
        f"metercalc-ledger-{month}.csv",
    )


@app.get("/api/runs/{run_id}/review.csv")
def api_run_review(run_id: str) -> Response:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    return _csv_response(summary.review_csv(run["review"]), f"metercalc-review-{run['month']}.csv")


@app.get("/api/ledger.csv")
def api_ledger() -> Response:
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc
    return _csv_response(summary.ledger_csv(ledger["months"]), "metercalc-ledger-all.csv")


@app.get("/api/review.csv")
def api_review() -> Response:
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc
    return _csv_response(summary.review_csv(ledger["review"]), "metercalc-review-all.csv")
