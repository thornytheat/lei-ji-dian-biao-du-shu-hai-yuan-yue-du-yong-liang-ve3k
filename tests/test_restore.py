"""用量还原规则：正常做差、回绕、换表分段、无法判定异常隔离。"""

from __future__ import annotations

from datetime import date

from app import readings, restore

FULL = 99999.9
CHANGE = {
    "meter_id": "EM-02",
    "change_date": "2025-09-15",
    "old_meter_no": "DDZY-2014-00417",
    "new_meter_no": "DDZY-2025-01183",
    "segments": {
        "total": {"old_final": 3430.8, "new_initial": 0.0},
        "peak": {"old_final": 2127.1, "new_initial": 0.0},
        "valley": {"old_final": 1303.7, "new_initial": 0.0},
    },
}


def point(day: str, value: float) -> dict:
    return {"read_at": date.fromisoformat(day), "value": value}


def test_normal_interval_usage():
    result = restore.interval_usage(
        point("2026-03-01", 100.0), point("2026-03-02", 130.5),
        meter_id="EM-01", kind="total", full_scale=FULL, change=None,
    )
    assert result["status"] == "normal"
    assert result["usage"] == 30.5
    assert result["days"] == 1
    assert "130.5" in result["formula"] and "100" in result["formula"]


def test_rollover_restores_usage_from_full_scale():
    result = restore.interval_usage(
        point("2025-12-16", 99997.6), point("2025-12-17", 15.1),
        meter_id="EM-03", kind="total", full_scale=FULL, change=None,
    )
    assert result["status"] == "rollover"
    # 99999.9 - 99997.6 + 15.1 = 17.4
    assert result["usage"] == 17.4
    assert "99999.9" in result["formula"]


def test_meter_change_sums_two_segments():
    result = restore.interval_usage(
        point("2025-09-14", 3417.4), point("2025-09-16", 14.6),
        meter_id="EM-02", kind="total", full_scale=FULL, change=CHANGE,
    )
    assert result["status"] == "meter_change"
    # 旧表尾段 13.4 + 新表首段 14.6 = 28.0
    assert result["usage"] == 28.0
    assert len(result["segments"]) == 2
    assert result["segments"][0]["usage"] == 13.4
    assert result["segments"][1]["usage"] == 14.6
    assert result["segments"][0]["days"] + result["segments"][1]["days"] == result["days"] == 2
    assert result["change"]["old_meter_no"] == "DDZY-2014-00417"


def test_backward_reading_without_change_is_isolated():
    result = restore.interval_usage(
        point("2026-03-16", 8326.0), point("2026-03-17", 8015.0),
        meter_id="EM-04", kind="total", full_scale=FULL, change=None,
    )
    assert result["status"] == "anomaly"
    assert result["usage"] is None
    assert "无法判定" in result["reason"]


def test_small_backward_step_without_near_full_scale_is_not_rollover():
    result = restore.interval_usage(
        point("2025-06-01", 50000.0), point("2025-06-02", 100.0),
        meter_id="EM-99", kind="total", full_scale=FULL, change=None,
    )
    # 前读数远低于量程顶部，不能判回绕
    assert result["status"] == "anomaly"
    assert result["usage"] is None


def test_change_record_inconsistent_with_readings_is_isolated():
    broken = {
        "meter_id": "EM-02",
        "change_date": "2025-09-15",
        "segments": {"total": {"old_final": 3000.0, "new_initial": 0.0}},
    }
    result = restore.interval_usage(
        point("2025-09-14", 3417.4), point("2025-09-16", 14.6),
        meter_id="EM-02", kind="total", full_scale=FULL, change=broken,
    )
    assert result["status"] == "anomaly"
    assert result["usage"] is None
    assert "不自洽" in result["reason"]


def test_change_only_covers_interval_it_belongs_to():
    # 换表日等于区间起点时不覆盖（前端读数已是新表）
    assert restore.find_change([CHANGE], "EM-02", date(2025, 9, 15), date(2025, 9, 16)) is None
    assert restore.find_change([CHANGE], "EM-02", date(2025, 9, 14), date(2025, 9, 16)) is not None
    # 其它表不误用
    assert restore.find_change([CHANGE], "EM-03", date(2025, 9, 14), date(2025, 9, 16)) is None


def test_sample_rollover_and_change_are_recognised(sample):
    series = readings.group_series(sample["rows"])
    meters = sample["meters"]
    changes = sample["changes"]
    restored = restore.restore_all(series, meters, changes, full_scale=FULL, max_gap_days=31)

    em03 = restored["EM-03"]
    assert len(em03["rollovers"]) == 1
    assert em03["rollovers"][0]["usage"] == 17.4

    em02 = restored["EM-02"]
    assert len(em02["changes"]) == 3  # total / peak / valley 各一段换表区间
    total_change = [item for item in em02["changes"] if item["kind"] == "total"][0]
    assert total_change["usage"] == 28.0

    em04 = restored["EM-04"]
    assert len(em04["anomalies"]) == 1
    anomaly = em04["anomalies"][0]
    assert anomaly["start"] == "2026-03-16" and anomaly["end"] == "2026-03-17"
    assert sorted(anomaly["kinds"]) == ["peak", "total", "valley"]

    # 隔离区间不产生用量：EM-04 的 total 序列累计用量 = 各正常区间之和
    total_kind = em04["kinds"]["total"]
    assert total_kind["usage_total"] == round(sum(i["usage"] for i in total_kind["intervals"] if i["usage"]), 2)
    assert all(i["usage"] is None or i["usage"] >= 0 for i in total_kind["intervals"])


def test_gap_intervals_are_flagged_not_split(sample):
    series = readings.group_series(sample["rows"])
    restored = restore.restore_all(
        series, sample["meters"], sample["changes"],
        full_scale=FULL, max_gap_days=31,
        meter_gaps=readings.meter_gaps(series, 31),
    )
    em03_total = restored["EM-03"]["kinds"]["total"]["intervals"]
    long_ones = [item for item in em03_total if item["gap"]]
    assert len(long_ones) == 1
    assert long_ones[0]["days"] == 35
    # 缺口区间仍按真实读数做差，不加插值点
    assert long_ones[0]["usage"] == 781.6
