"""能量求值:把累计表差与变电所区间读数统一为带方向、质量和不确定范围的能量。

规则:
- 累计值重启或倒退时不直接相减:在重置点切断,只累加单调段;
  重置空窗内的消耗不可知,用上限速率扩展不确定区间上界。
- 相邻采样倍率变化时切断(由切分服务保证片段内倍率一致,这里仍做防御性切分)。
- 通信 LOST 区间视为空窗,同样只扩上界、不虚构消耗。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..config import MAX_AUX_RATE_KWH_PER_MIN, MAX_TRACTION_RATE_KWH_PER_MIN
from ..models import MeterReading

EPS = 1e-9


@dataclass
class CounterPiece:
    """一段单调、同倍率的计数器区间。"""
    start: datetime
    end: datetime
    start_val: float
    end_val: float
    multiplier: float

    @property
    def kwh(self) -> float:
        return (self.end_val - self.start_val) * self.multiplier

    @property
    def minutes(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 60.0)


@dataclass
class CounterResult:
    value: float = 0.0                     # 单调段累计(已乘倍率)
    lo: float = 0.0                        # 下界 = 已确认消耗
    hi: Optional[float] = 0.0              # 上界;None = 不闭合(全程无有效读数)
    pieces: list = field(default_factory=list)      # List[CounterPiece]
    resets: list = field(default_factory=list)      # 重启/倒退事件
    gaps: list = field(default_factory=list)        # LOST 空窗
    multiplier_changed: bool = False
    coverage: float = 0.0

    def band(self):
        if self.hi is None:
            # 窗口内无有效读数:未知,不闭合(禁止用 0 冒充)
            return {"v": None, "lo": None, "hi": None}
        return {"v": round(self.value, 3), "lo": round(self.lo, 3),
                "hi": round(self.hi, 3)}


def diff_counter(readings: list[MeterReading], field_name: str,
                 win_start: datetime, win_end: datetime,
                 max_rate_per_min: float) -> CounterResult:
    """对 [win_start, win_end] 内某累计字段求差,处理重启/倒退/倍率变化/失连。

    readings 需已按 (sampled_at, seq) 排序且属于同一设备。
    """
    res = CounterResult()
    rows = [r for r in readings
            if getattr(r, field_name) is not None
            and r.comm_status != "LOST"
            and win_start <= r.sampled_at <= win_end]
    if not rows:
        res.hi = None  # 窗口内无有效读数:未知,不闭合
        return res

    total_min = max((win_end - win_start).total_seconds() / 60.0, EPS)
    covered = 0.0
    prev = None
    for r in rows:
        val = getattr(r, field_name)
        if prev is not None:
            gap_min = (r.sampled_at - prev.sampled_at).total_seconds() / 60.0
            if r.multiplier != prev.multiplier:
                # 倍率变化:切断,旧段闭合,新段从当前读数重新起算
                res.multiplier_changed = True
                res.pieces.append(CounterPiece(prev.sampled_at, prev.sampled_at,
                                               getattr(prev, field_name), getattr(prev, field_name),
                                               prev.multiplier))
                prev = r
                continue
            if val < getattr(prev, field_name) - EPS:
                # 重启/倒退:禁止直接相减。旧段已在上一轮闭合;
                # 空窗消耗不可知 -> 上界按最大速率扩展
                res.resets.append({
                    "at": r.sampled_at.isoformat(), "seq": r.seq,
                    "dropped_from": getattr(prev, field_name), "restarted_at": val,
                    "gap_minutes": round(gap_min, 2),
                })
                res.hi = (res.hi or 0.0) + max_rate_per_min * gap_min
                covered += gap_min  # 空窗已扩上界,避免末尾未覆盖段重复扩展
                prev = r
                continue
            piece = CounterPiece(prev.sampled_at, r.sampled_at,
                                 getattr(prev, field_name), val, r.multiplier)
            res.pieces.append(piece)
            res.value += piece.kwh
            res.lo += piece.kwh
            res.hi = (res.hi or 0.0) + piece.kwh
            covered += piece.minutes
        prev = r

    # 窗口边缘未被读数覆盖的部分:扩上界(下界不变)
    uncovered = max(0.0, total_min - covered)
    if res.hi is not None and uncovered > EPS:
        res.hi += max_rate_per_min * uncovered
    res.coverage = min(1.0, covered / total_min)
    return res


def band_sub(a: dict, b: dict) -> dict:
    """区间减法 a-b;任一侧未知则结果未知。"""
    if a["v"] is None or b["v"] is None or a["hi"] is None or b["hi"] is None:
        return {"v": None, "lo": None, "hi": None}
    return {"v": round(a["v"] - b["v"], 3),
            "lo": round(a["lo"] - b["hi"], 3),
            "hi": round(a["hi"] - b["lo"], 3)}


def band_add(a: dict, b: dict) -> dict:
    if a["v"] is None or b["v"] is None or a["hi"] is None or b["hi"] is None:
        return {"v": None, "lo": None, "hi": None}
    return {"v": round(a["v"] + b["v"], 3),
            "lo": round(a["lo"] + b["lo"], 3),
            "hi": round(a["hi"] + b["hi"], 3)}


def band_sum(bands: list[dict]) -> dict:
    out = {"v": 0.0, "lo": 0.0, "hi": 0.0}
    for b in bands:
        out = band_add(out, b)
        if out["v"] is None:
            return out
    return out
