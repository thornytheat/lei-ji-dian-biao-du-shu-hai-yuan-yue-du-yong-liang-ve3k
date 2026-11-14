"""补录 / 迟到更正：整批校验、只重算受影响月份、原子发布、旧版保留、幂等重试。"""

from __future__ import annotations

import pytest

from app import corrections, publish, store


@pytest.fixture(autouse=True)
def reset_sample(client):
    """每个用例从样例基线开始，避免补录改动串到其它用例。"""
    client.post("/api/import/sample")
    yield
    client.post("/api/import/sample")


def _run(client, month="2026-03"):
    return client.post("/api/restore", json={"month": month}).json()


def _run_data(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()["run"]


def _row(run, month, meter_id):
    return [row for row in run["ledger"][month]["rows"] if row["meter_id"] == meter_id][0]


def _csv_values(response):
    import csv
    import io

    return list(csv.reader(io.StringIO(response.text)))


def test_correction_updates_every_surface_from_one_version(client, monkeypatch):
    run = _run(client)
    run_id = run["run_id"]
    before = _run_data(client, run_id)
    before_total = _row(before, "2026-03", "EM-01")["total"]
    old_version_id = before["version_id"]

    body = {
        "readings": [
            {"meter_id": "EM-01", "read_at": "2026-03-29", "reading_kwh": 9700.0, "kind": "total"}
        ]
    }
    response = client.post(f"/api/runs/{run_id}/corrections", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["affected_months"] == ["2026-03"]
    assert payload["recomputed_months"] == ["2026-03"]
    assert len(payload["unchanged_months"]) == 11
    assert payload["version_id"].endswith("-v2")

    after = _run_data(client, run_id)
    assert after["revision"] == 2
    new_total = _row(after, "2026-03", "EM-01")["total"]
    assert new_total != before_total
    assert [_row(after, "2026-03", "EM-01")] == [
        row for row in after["focus"]["rows"] if row["meter_id"] == "EM-01"
    ]

    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    assert f"{new_total:g}" in page.text
    assert "v2" in page.text

    run_csv = client.get(f"/api/runs/{run_id}/ledger.csv")
    assert f"{new_total:.2f}" in run_csv.text
    full_csv = client.get("/api/ledger.csv")
    assert f"{new_total:.2f}" in full_csv.text
    review_csv = client.get("/api/review.csv")
    assert review_csv.status_code == 200

    # 旧版本快照仍在，可原样回读
    old = client.get(f"/api/runs/{run_id}/versions/{old_version_id}")
    assert old.status_code == 200
    assert _row(old.json()["run"], "2026-03", "EM-01")["total"] == before_total


def test_correction_only_touches_affected_month(client):
    run = _run(client)
    run_id = run["run_id"]
    before = _run_data(client, run_id)
    response = client.post(
        f"/api/runs/{run_id}/corrections",
        json={"readings": [{"meter_id": "EM-03", "read_at": "2026-02-20", "reading_kwh": 1600.0, "kind": "total"}]},
    )
    assert response.status_code == 200
    assert response.json()["affected_months"] == ["2026-02", "2026-03"]
    after = _run_data(client, run_id)

    untouched = [m for m in before["month_order"] if m not in {"2026-02", "2026-03"}]
    for month in untouched:
        assert before["ledger"][month] == after["ledger"][month]

    assert _row(before, "2026-02", "EM-03")["total"] != _row(after, "2026-02", "EM-03")["total"]
    assert after["stats"]["months"] == before["stats"]["months"]


def test_invalid_correction_batch_is_rejected_atomically(client):
    run = _run(client)
    run_id = run["run_id"]
    before = _run_data(client, run_id)
    bad = {
        "readings": [
            {"meter_id": "EM-01", "read_at": "2026-03-11", "reading_kwh": "abc", "kind": "total"},
            {"meter_id": "EM-01", "read_at": "2026-02-30", "reading_kwh": 1.0, "kind": "total"},
            {"meter_id": "EM-99", "read_at": "2026-03-12", "reading_kwh": 1.0, "kind": "total"},
            {"meter_id": "EM-01", "read_at": "2026-03-12", "reading_kwh": 1.0, "kind": "kwh"},
            {"meter_id": "EM-01", "read_at": "2026-03-13", "reading_kwh": 1.0, "kind": "total"},
            {"meter_id": "EM-01", "read_at": "2026-03-13", "reading_kwh": 2.0, "kind": "total"},
        ]
    }
    response = client.post(f"/api/runs/{run_id}/corrections", json=bad)
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert len(body["problems"]) >= 5
    assert "不是数字" in " ".join(item["message"] for item in body["problems"])
    assert "日历" in " ".join(item["message"] for item in body["problems"])
    assert "不存在" in " ".join(item["message"] for item in body["problems"])
    assert "重复" in " ".join(item["message"] for item in body["problems"])

    # 整批未应用：读数、批次版本、台账都没变
    after = _run_data(client, run_id)
    assert after["revision"] == 1
    assert after["ledger"] == before["ledger"]
    assert isinstance(client.get("/api/runs/%s" % run_id).json()["run"]["ledger"], dict)


def test_failed_publish_keeps_old_version_then_retry_reuses_batch(client, monkeypatch):
    run = _run(client)
    run_id = run["run_id"]
    before = _run_data(client, run_id)
    body = {"readings": [{"meter_id": "EM-01", "read_at": "2026-03-29", "reading_kwh": 9700.0, "kind": "total"}]}

    original = publish.store.publish_version

    def boom(*args, **kwargs):
        raise RuntimeError("simulated publish failure")

    monkeypatch.setattr(publish.store, "publish_version", boom)
    failed = client.post(f"/api/runs/{run_id}/corrections", json=body)
    assert failed.status_code == 500
    assert "旧版本" in failed.json()["error"]

    # 旧版本继续对外发布：版本号、台账、导出都还是旧的
    after_fail = _run_data(client, run_id)
    assert after_fail["revision"] == 1
    assert after_fail["version_id"] == before["version_id"]
    assert after_fail["ledger"] == before["ledger"]
    assert client.get("/api/ledger.csv").status_code == 200

    monkeypatch.setattr(publish.store, "publish_version", original)
    retry = client.post(f"/api/runs/{run_id}/corrections", json=body)
    assert retry.status_code == 200
    assert retry.json()["version_id"].endswith("-v2")

    repeat = client.post(f"/api/runs/{run_id}/corrections", json=body)
    assert repeat.status_code == 200
    assert repeat.json()["idempotent"] is True
    assert repeat.json()["version_id"] == retry.json()["version_id"]

    versions = client.get(f"/api/runs/{run_id}/versions").json()["versions"]
    assert len(versions) == 2
    corrections_list = client.get(f"/api/runs/{run_id}/corrections").json()["corrections"]
    assert len(corrections_list) == 1
    assert corrections_list[0]["status"] == "applied"


def test_correction_unknown_run(client):
    response = client.post(
        "/api/runs/run-does-not-exist/corrections",
        json={"readings": [{"meter_id": "EM-01", "read_at": "2026-03-01", "reading_kwh": 1.0, "kind": "total"}]},
    )
    assert response.status_code == 404


def test_affected_months_include_calendar_split():
    from datetime import date

    rows = [
        {"meter_id": "M", "read_at": date(2026, 2, 20), "reading_kwh": 100.0, "kind": "total"},
        {"meter_id": "M", "read_at": date(2026, 3, 27), "reading_kwh": 881.6, "kind": "total"},
    ]
    corrections_rows = [
        {"meter_id": "M", "read_at": date(2026, 2, 20), "reading_kwh": 160.0, "kind": "total", "line": 1}
    ]
    months = corrections.affected_months(corrections_rows, rows)
    assert months == ["2026-02", "2026-03"]


def test_apply_to_rows_counts_actual_changes():
    from datetime import date

    existing = [
        {"meter_id": "M", "read_at": date(2026, 3, 1), "reading_kwh": 100.0, "kind": "total"},
    ]
    same = [{"meter_id": "M", "read_at": date(2026, 3, 1), "reading_kwh": 100.0, "kind": "total"}]
    changed = [{"meter_id": "M", "read_at": date(2026, 3, 1), "reading_kwh": 120.0, "kind": "total"}]
    added = [{"meter_id": "M", "read_at": date(2026, 3, 2), "reading_kwh": 130.0, "kind": "total"}]
    rows, count = corrections.apply_to_rows(existing, same)
    assert count == 0 and len(rows) == 1
    rows, count = corrections.apply_to_rows(existing, changed)
    assert count == 1 and rows[0]["reading_kwh"] == 120.0
    rows, count = corrections.apply_to_rows(existing, added)
    assert count == 1 and len(rows) == 2
