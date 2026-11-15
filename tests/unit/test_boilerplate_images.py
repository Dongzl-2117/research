"""审核 C7 的只读对照：两种判法各算一遍。合成时刻，不连库。"""

from datetime import UTC, datetime, timedelta

from researchlib.boilerplate import IMAGE_COVERAGE
from researchlib.boilerplate_images import densest_coverage, overall_coverage

START = datetime(2025, 1, 1, tzinfo=UTC)


def _days(*numbers):
    return [START + timedelta(days=n) for n in numbers]


def test_a_coupon_reused_once_much_later_flips_now_but_not_by_the_densest_stretch():
    published = _days(*range(64))  # 每天一篇
    coupon = _days(*range(0, 33, 3), 63)[:10] + _days(63)  # 前 33 天里 10 篇，第 64 天又贴一次
    assert overall_coverage(coupon, published) < IMAGE_COVERAGE
    assert densest_coverage(coupon, published) >= IMAGE_COVERAGE


def test_a_diagram_reused_every_few_months_stays_occasional():
    published = _days(*range(0, 1000))
    diagram = _days(0, 150, 300, 450, 600, 750, 900)
    assert overall_coverage(diagram, published) < IMAGE_COVERAGE
    assert densest_coverage(diagram, published) < IMAGE_COVERAGE
