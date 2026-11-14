"""月度分摊：按天数比例、2 位小数、合计守恒、月边界处理。"""

from __future__ import annotations

from datetime import date

import pytest

from app import proration


def test_split_months_days_add_up():
    buckets = proration.split_months(date(2026, 2, 20), date(2026, 3, 27))
    assert [bucket["month"] for bucket in buckets] == ["2026-02", "2026-03"]
    assert [bucket["days"] for bucket in buckets] == [9, 26]  # 2/20~2/28 + 3/1~3/26 = 35 天
    assert sum(bucket["days"] for bucket in buckets) == 35


def test_cross_month_proration_conserves_usage():
    split = proration.prorate(date(2026, 2, 20), date(2026, 3, 27), 781.6)
    shares = [part["share"] for part in split["parts"]]
    assert len(shares) == 2
    assert round(sum(shares), 2) == 781.6
    assert all(round(share, 2) == share for share in shares)  # 全部 2 位小数
    assert split["parts"][1]["share"] == 580.62  # 781.6 × 26/35
    assert split["parts"][0]["share"] == 200.98
    assert split["balanced"] is True
    assert "26/35" in split["formula"]


def test_three_month_span_conserves_usage_with_largest_remainder():
    split = proration.prorate(date(2026, 1, 20), date(2026, 4, 10), 100.0)
    months = [part["month"] for part in split["parts"]]
    assert months == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert round(sum(part["share"] for part in split["parts"]), 2) == 100.0
    # 100 元分摊到 4 个月，尾差必须被吸收，不能出现 99.99 / 100.01
    assert split["residual_cents"] == 0 or abs(sum(part["share"] for part in split["parts"]) - 100.0) < 1e-9


def test_single_month_interval_has_weight_one():
    split = proration.prorate(date(2026, 3, 1), date(2026, 3, 31), 660.44)
    assert len(split["parts"]) == 1
    assert split["parts"][0]["share"] == 660.44
    assert split["parts"][0]["weight"] == 1.0


def test_month_boundary_belongs_to_earlier_month():
    # [2026-02-28, 2026-03-01) 只有一天，属于 2 月
    split = proration.prorate(date(2026, 2, 28), date(2026, 3, 1), 12.3)
    assert [part["month"] for part in split["parts"]] == ["2026-02"]
    assert split["parts"][0]["days"] == 1


def test_year_boundary_split():
    buckets = proration.split_months(date(2025, 12, 30), date(2026, 1, 2))
    assert [(bucket["month"], bucket["days"]) for bucket in buckets] == [("2025-12", 2), ("2026-01", 1)]


def test_parse_month_validates_format():
    assert proration.parse_month("2026-03") == (date(2026, 3, 1), date(2026, 4, 1))
    assert proration.parse_month("2025-12")[1] == date(2026, 1, 1)
    for bad in ("2026-13", "2026-3", "202603", "2026/03", ""):
        with pytest.raises(ValueError):
            proration.parse_month(bad)


def test_allocate_monthly_totals_match_interval_usage(sample):
    from app import readings, restore

    series = readings.group_series(sample["rows"])
    restored = restore.restore_all(series, sample["meters"], sample["changes"], full_scale=99999.9)

    total_intervals = []
    for meter_id, entry in restored.items():
        total_intervals.extend(entry["kinds"]["total"]["intervals"])

    allocation = proration.allocate(total_intervals)
    monthly_sum = round(sum(allocation["by_month"].values()), 2)
    interval_sum = round(sum(item["usage"] for item in total_intervals if item["usage"] is not None), 2)
    assert monthly_sum == interval_sum
    # 每个区间的分摊合计都必须等于区间用量本身（误差 ≤ 0.01）
    for evidence in allocation["evidence"]:
        parts_sum = round(sum(part["share"] for part in evidence["proration"]["parts"]), 2)
        assert abs(parts_sum - evidence["usage"]) <= 0.01
