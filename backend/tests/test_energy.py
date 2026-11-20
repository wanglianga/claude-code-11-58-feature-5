"""计数器求值单元测试:重启/倒退不直接相减、倍率切断、未知范围。"""
from datetime import datetime, timedelta

from app.services.energy import band_add, band_sub, diff_counter

T0 = datetime(2026, 10, 20, 8, 0)


class R:
    def __init__(self, minutes, val, mult=1.0, comm="OK"):
        self.sampled_at = T0 + timedelta(minutes=minutes)
        self.seq = minutes
        self.traction_cum = val
        self.multiplier = mult
        self.comm_status = comm


def test_normal_monotonic_diff():
    rows = [R(0, 100), R(10, 105), R(20, 112)]
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=20), 40)
    assert res.value == 12 and res.lo == 12
    assert res.coverage == 1.0 and not res.resets


def test_reset_not_subtracted():
    """重启后读数归零:禁止直接相减,只累计单调段,空窗扩上界。"""
    rows = [R(0, 100), R(10, 108), R(15, 0.5), R(20, 3.5)]  # 15min 处重启
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=20), 40)
    assert res.value == 8 + 3          # 两段单调段,不含负跳变
    assert len(res.resets) == 1
    assert res.hi > res.lo             # 空窗 5min 按最大速率扩上界
    assert res.band()["hi"] == res.lo + 40 * 5


def test_rollback_treated_as_reset():
    rows = [R(0, 100), R(10, 90)]  # 倒退(非归零)
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=10), 40)
    assert res.value == 0 and len(res.resets) == 1


def test_multiplier_change_cuts():
    rows = [R(0, 100, mult=1.0), R(10, 110, mult=1.0), R(20, 115, mult=2.0)]
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=20), 40)
    assert res.multiplier_changed
    assert res.value == 10  # 倍率切换点切断,切换区间不计


def test_multiplier_applied():
    rows = [R(0, 100, mult=2.0), R(10, 110, mult=2.0)]
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=10), 40)
    assert res.value == 20  # 10 个表码 × 倍率 2


def test_empty_window_is_unknown_not_zero():
    res = diff_counter([], "traction_cum", T0, T0 + timedelta(minutes=10), 40)
    band = res.band()
    assert band == {"v": None, "lo": None, "hi": None}  # 未知,禁止冒充 0


def test_lost_readings_excluded():
    rows = [R(0, 100), R(10, 105, comm="LOST"), R(20, 112)]
    res = diff_counter(rows, "traction_cum", T0, T0 + timedelta(minutes=20), 40)
    assert res.value == 12  # LOST 行不参与


def test_band_arithmetic_unknown_propagates():
    known = {"v": 10.0, "lo": 9.0, "hi": 11.0}
    unknown = {"v": None, "lo": None, "hi": None}
    assert band_add(known, unknown)["v"] is None
    assert band_sub(known, unknown)["hi"] is None
    assert band_sub(known, known) == {"v": 0.0, "lo": -2.0, "hi": 2.0}
