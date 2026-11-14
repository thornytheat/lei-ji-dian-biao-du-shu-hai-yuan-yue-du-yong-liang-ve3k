"""分时一致性容差与缺口判定：不插值、缺口天数按月份重叠计算。"""

from __future__ import annotations

from datetime import date

from app import readings, restore, validation


def test_time_of_use_within_tolerance():
    check = validation.time_of_use_check(660.0, 409.2, 250.8)
    assert check["ok"] is True
    assert check["diff"] == 0.0
    assert check["status"] == "ok"
    assert "通过" in check["note"]


def test_time_of_use_exact_tolerance_boundary_passes():
    # 容差 0.5%：总量 1000，允许 ±5.0
    check = validation.time_of_use_check(1000.0, 620.0, 385.0)
    assert check["diff"] == 5.0
    assert check["allowed"] == 5.0
    assert check["ok"] is True

    just_over = validation.time_of_use_check(1000.0, 620.0, 385.1)
    assert just_over["ok"] is False
    assert just_over["status"] == "mismatch"


def test_time_of_use_mismatch_still_returns_numbers():
    check = validation.time_of_use_check(727.2, 401.7, 276.3)
    assert check["ok"] is False
    assert check["diff"] == -49.2
    assert round(check["deviation_ratio"], 4) == round(-49.2 / 727.2, 4)
    # 超差仍输出三个数值，不允许吞掉
    assert (check["total"], check["peak"], check["valley"]) == (727.2, 401.7, 276.3)
    assert "超差" in check["note"]


def test_time_of_use_missing_component():
    check = validation.time_of_use_check(100.0, None, 40.0)
    assert check["ok"] is False
    assert check["status"] == "missing"
    assert check["sum"] is None


def test_gap_threshold_is_strictly_greater_than_31_days():
    points = [
        {"read_at": date(2025, 1, 1), "value": 100.0},
        {"read_at": date(2025, 2, 1), "value": 200.0},   # 31 天：不算缺口
        {"read_at": date(2025, 3, 5), "value": 300.0},   # 32 天：缺口
    ]
    gaps = readings.detect_gaps(points, 31)
    assert len(gaps) == 1
    assert gaps[0]["start"] == date(2025, 2, 1)
    assert gaps[0]["end"] == date(2025, 3, 5)
    assert gaps[0]["span_days"] == 32
    assert gaps[0]["missing_days"] == 31
    assert gaps[0]["suggested_read_at"] == "2025-02-17"
    assert "不插值" in gaps[0]["note"]


def test_gap_does_not_inject_interpolated_points(sample):
    series = readings.group_series(sample["rows"])
    em03 = series[("EM-03", "total")]
    before = len(em03)
    gaps = readings.detect_gaps(em03, 31)
    assert len(gaps) == 1
    assert gaps[0]["span_days"] == 35
    # 检测缺口不改变原始序列长度（纯函数，不插值）
    assert len(em03) == before
    restored = restore.restore_series(
        "EM-03", "total", em03, changes=[], full_scale=99999.9, max_gap_days=31
    )
    assert len(restored["intervals"]) == len(em03) - 1
    long_interval = [item for item in restored["intervals"] if item["gap"]][0]
    # 区间用量仍是真实读数差，没有人为补点
    assert long_interval["usage"] == round(gaps[0]["next_value"] - gaps[0]["prev_value"], 2)


def test_gap_days_in_month_uses_overlap():
    gap = {
        "start": date(2026, 2, 20),
        "end": date(2026, 3, 27),
        "span_days": 35,
        "missing_days": 34,
    }
    feb_start, feb_end = date(2026, 2, 1), date(2026, 3, 1)
    mar_start, mar_end = date(2026, 3, 1), date(2026, 4, 1)
    assert validation.gap_days_in_month(gap, feb_start, feb_end) == 8   # 2/21~2/28
    assert validation.gap_days_in_month(gap, mar_start, mar_end) == 26  # 3/1~3/26
    assert validation.gap_days_in_month(gap, date(2026, 4, 1), date(2026, 5, 1)) == 0


def test_coverage_note_mentions_no_interpolation():
    note = validation.coverage_note(781.6, 35, 34)
    assert "不插值" in note
    assert "22.33" in note
    assert "不插值" not in validation.coverage_note(660.0, 31, 0)
