"""HTTP 层：成功路径、4xx 文案、端到端（导入 → 还原 → 结果页 → 导出）。"""

from __future__ import annotations

import csv
import io

BAD_CSV = """meter_id,read_at,reading_kwh,kind
EM-01,2026-02-30,1200.0,total
EM-01,2026-03-01,abc,total
EM-09,2026-03-01,100.0,kwh
"""

SMALL_CSV = """meter_id,read_at,reading_kwh,kind
WT-01,2026-01-01,100.0,total
WT-01,2026-01-01,62.0,peak
WT-01,2026-01-01,38.0,valley
WT-01,2026-02-01,160.0,total
WT-01,2026-02-01,99.2,peak
WT-01,2026-02-01,60.8,valley
"""


def test_health_reports_seeded_sample(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "metercalc"
    assert body["readings"] == 4140
    assert body["meters"] == 4
    assert body["changes"] == 1
    assert body["readings_from"] == "2025-04-01"
    assert body["readings_to"] == "2026-03-31"


def test_pages_render(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "累计电表读数" in index.text
    assert "演示步骤" in index.text

    workbench = client.get("/workbench")
    assert workbench.status_code == 200
    assert "执行还原" in workbench.text

    review = client.get("/review")
    assert review.status_code == 200
    assert "复核清单" in review.text
    assert "EM-03" in review.text


def test_restore_march_returns_real_numbers(client):
    response = client.post("/api/restore", json={"month": "2026-03"})
    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["month"] == "2026-03"
    assert set(body["meters"]) == {"EM-01", "EM-02", "EM-03", "EM-04"}
    rows = {row["meter_id"]: row for row in body["rows"]}
    assert rows["EM-01"]["total"] == 727.2
    assert rows["EM-03"]["total"] == 653.12      # 跨月区间按 26/35 天分摊
    assert rows["EM-04"]["total"] == 367.6
    assert rows["EM-01"]["anomaly_count"] == 1   # 分时超差
    assert rows["EM-04"]["anomaly_count"] == 1   # 隔离异常
    assert rows["EM-03"]["gap_days"] == 26
    assert body["totals"]["usage_total"] == 2225.82
    assert body["totals"]["anomaly_count"] == 2
    assert body["totals"]["gap_segments"] == 1
    assert len(body["gaps"]) == 1
    assert body["gaps"][0]["suggested_read_at"] == "2026-03-09"
    assert len(body["anomalies"]) == 1
    assert body["anomalies"][0]["meter_id"] == "EM-04"
    assert body["result_url"].startswith("/result/run-")


def test_result_page_shows_evidence_and_proration(client):
    run = client.post("/api/restore", json={"month": "2026-03"}).json()
    page = client.get(run["result_url"])
    assert page.status_code == 200
    html = page.text
    assert "月度用能还原结果" in html
    assert "727.2" in html
    # 逐条依据：区间起止与分摊公式
    assert "2026-02-20 → 2026-03-27" in html
    assert "26/35" in html
    assert "回绕" in html and "换表分段" in html
    assert "分时一致性校验" in html
    assert "禁止插值" in html
    assert "复核清单" in html


def test_restore_change_month_and_rollover_month(client):
    september = client.post("/api/restore", json={"month": "2025-09"}).json()
    assert len(september["changes"]) >= 1
    total_change = [item for item in september["changes"] if "总" not in item["formula"]][0]
    assert total_change["meter_id"] == "EM-02"
    assert any(item["usage"] == 28.0 for item in september["changes"])

    december = client.post("/api/restore", json={"month": "2025-12"}).json()
    assert december["rollovers"][0]["usage"] == 17.4
    assert "99999.9" in december["rollovers"][0]["formula"]


def test_restore_rejects_unknown_meter(client):
    response = client.post("/api/restore", json={"month": "2026-03", "meter_ids": ["EM-99"]})
    assert response.status_code == 400
    assert "表号不存在" in response.json()["detail"]
    assert "EM-01" in response.json()["detail"]


def test_restore_rejects_bad_month(client):
    bad_format = client.post("/api/restore", json={"month": "2026-13"})
    assert bad_format.status_code == 400
    assert "月份" in bad_format.json()["detail"]

    no_data = client.post("/api/restore", json={"month": "2020-01"})
    assert no_data.status_code == 400
    assert "没有任何读数覆盖" in no_data.json()["detail"]


def test_restore_rejects_bad_scale_and_gap(client):
    negative = client.post("/api/restore", json={"month": "2026-03", "full_scale": -1})
    assert negative.status_code == 400
    assert "量程" in negative.json()["detail"]

    gap = client.post("/api/restore", json={"month": "2026-03", "max_gap_days": 1})
    assert gap.status_code == 400
    assert "缺口阈值" in gap.json()["detail"]


def test_unknown_run_page_and_api(client):
    assert client.get("/result/run-does-not-exist").status_code == 200
    assert "不存在" in client.get("/result/run-does-not-exist").text
    assert client.get("/api/runs/run-does-not-exist").status_code == 404


def test_import_rejects_broken_csv_with_line_numbers(client):
    response = client.post(
        "/api/import",
        files={"file": ("broken.csv", BAD_CSV.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert len(body["problems"]) >= 3
    messages = "；".join(item["message"] for item in body["problems"])
    assert "2026-02-30" in messages or "日历" in messages
    assert "不是数字" in messages
    assert "未知电量类型" in messages


def test_idempotent_sample_import(client):
    first = client.post("/api/import/sample").json()
    second = client.post("/api/import/sample").json()
    assert first["counts"]["readings"] == 4140
    assert second["counts"]["readings"] == 4140
    assert second["counts"]["readings"] == first["counts"]["readings"]


def test_end_to_end_import_restore_export(client):
    upload = client.post(
        "/api/import",
        files={"file": ("small.csv", SMALL_CSV.encode("utf-8"), "text/csv")},
    )
    assert upload.status_code == 200
    assert upload.json()["new_meters"] == ["WT-01"]

    restore = client.post("/api/restore", json={"month": "2026-01", "meter_ids": ["WT-01"]})
    assert restore.status_code == 201
    body = restore.json()
    assert body["rows"][0]["meter_id"] == "WT-01"
    assert body["rows"][0]["total"] == 60.0
    assert body["rows"][0]["status"] == "ok"

    ledger = client.get(f"/api/runs/{body['run_id']}/ledger.csv")
    assert ledger.status_code == 200
    assert "attachment" in ledger.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(ledger.text)))
    assert rows[0][0] == "月份" and rows[1][1] == "WT-01"

    detail = client.get(f"/api/runs/{body['run_id']}").json()
    assert detail["run"]["focus"]["totals"]["usage_total"] == 60.0

    # 恢复样例，避免影响其它用例
    restored = client.post("/api/import/sample").json()
    assert restored["counts"]["readings"] == 4140


def test_review_exports(client):
    client.post("/api/restore", json={"month": "2026-03"})
    ledger = client.get("/api/ledger.csv")
    assert ledger.status_code == 200
    rows = list(csv.reader(io.StringIO(ledger.text)))
    assert rows[0][:4] == ["月份", "表号", "表名", "总电量kWh"]
    assert len(rows) > 40  # 12 个月 × 4 块表左右

    review = client.get("/api/review.csv")
    assert review.status_code == 200
    review_rows = list(csv.reader(io.StringIO(review.text)))
    assert review_rows[0][0] == "月份"
    assert any("分时超差" in row[4] for row in review_rows[1:])
    assert any("读数回退" in row[4] for row in review_rows[1:])
