"""账实核对:计算列车侧与供电侧的可比较范围、差异去向与不可判定原因。

两种路径(运行对齐 / 网格对齐)都输出同一运行范围内的可比较能耗与差异边界,
互为独立实现而非事后校验。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import COVERAGE_THRESHOLD, LINE_LOSS_RATE
from ..models import (ConfigVersion, EnergyFragment, GridCell, MeterReading,
                      Reconciliation, Run, RunSegment)
from .energy import band_add, band_sub, band_sum

ZERO = {"v": 0.0, "lo": 0.0, "hi": 0.0}
UNKNOWN = {"v": None, "lo": None, "hi": None}


def _net_onboard(quantities: dict) -> dict:
    """车载净能耗 = 牵引 + 辅助 - 回馈(方向已展开)。"""
    t = quantities.get("traction", UNKNOWN)
    a = quantities.get("aux", UNKNOWN)
    r = quantities.get("regen", UNKNOWN)
    pos = band_add(t, a)
    return band_sub(pos, r)


def _net_supply(quantities: dict) -> dict:
    """供电净受电 = 受电 - 返送。"""
    return band_sub(quantities.get("import", UNKNOWN), quantities.get("export", UNKNOWN))


def _regen_accepted(db: Session, run: Run) -> bool:
    """电网是否接受该运行所经区段的回馈。配置 payload:
    {"default": true, "stations": {"SUB-S3": false}}"""
    cv = (db.query(ConfigVersion)
          .filter(ConfigVersion.kind == "REGEN_ACCEPT",
                  ConfigVersion.valid_from <= run.started_at)
          .order_by(ConfigVersion.valid_from.desc()).first())
    if not cv:
        return True
    payload = cv.payload or {}
    segs = (db.query(RunSegment).filter(RunSegment.run_id == run.id).all())
    from ..models import Device, Section
    station_serials = set()
    for s in segs:
        sec = db.get(Section, s.section_id)
        if sec and sec.substation_device_id:
            dev = db.get(Device, sec.substation_device_id)
            if dev:
                station_serials.add(dev.serial)
    overrides = payload.get("stations", {})
    for serial in station_serials:
        if overrides.get(serial) is False:
            return False
    return bool(payload.get("default", True))


def _late_reading_delay(db: Session, run: Run) -> float:
    """边界附近迟到采样的最大延迟(分钟),用于计量延迟归因。"""
    from ..models import Device
    devs = (db.query(Device)
            .filter((Device.train_id == run.train_id) | (Device.kind == "SUBSTATION")).all())
    ids = [d.id for d in devs]
    if not ids:
        return 0.0
    rows = (db.query(MeterReading)
            .filter(MeterReading.device_id.in_(ids),
                    MeterReading.sampled_at >= run.started_at,
                    MeterReading.sampled_at <= run.ended_at).all())
    delays = [(r.received_at - r.sampled_at).total_seconds() / 60.0 for r in rows]
    return round(max(delays), 1) if delays else 0.0


def _attribute(db: Session, run: Run, onboard_quantities: list[dict],
               supply_quantities: list[dict], diff: dict) -> dict:
    """差异去向归因:线损 / 回馈未上网 / 计量延迟 / 不可知。

    各项按顺序从差异总额中扣除,保证残差非负、各项之和等于 diff。
    """
    assumptions = {"line_loss_rate": LINE_LOSS_RATE,
                   "regen_accepted": _regen_accepted(db, run),
                   "max_late_delay_min": _late_reading_delay(db, run)}
    if diff["v"] is None:
        assumptions["note"] = "差异未知(一侧不闭合),不做归因"
        return {"line_loss": 0.0, "regen_not_returned": 0.0, "metering_delay": 0.0,
                "unknown": 0.0, "assumptions": assumptions}

    supply_import = band_sum([q.get("import", ZERO) for q in supply_quantities])
    supply_export = band_sum([q.get("export", ZERO) for q in supply_quantities])
    onboard_regen = band_sum([q.get("regen", ZERO) for q in onboard_quantities])

    remaining = max(0.0, diff["v"])
    line_loss = min(remaining, round((supply_import["v"] or 0.0) * LINE_LOSS_RATE, 3))
    remaining = round(remaining - line_loss, 3)

    regen_pool = max(0.0, (onboard_regen["v"] or 0.0) - (supply_export["v"] or 0.0))
    if not _regen_accepted(db, run):
        # 电网不接受回馈:车载回馈被制动电阻消耗,全部计入差异
        regen_pool = onboard_regen["v"] or 0.0
    regen_not_returned = min(remaining, round(regen_pool, 3))
    remaining = round(remaining - regen_not_returned, 3)

    delay_min = assumptions["max_late_delay_min"]
    metering_delay = min(remaining, round(delay_min * 0.5, 3)) if delay_min > 5 else 0.0
    remaining = round(remaining - metering_delay, 3)

    return {"line_loss": line_loss, "regen_not_returned": regen_not_returned,
            "metering_delay": metering_delay, "unknown": remaining,
            "assumptions": assumptions}


def reconcile_run_aligned(db: Session, run: Run) -> Reconciliation:
    """路径A:逐片段对齐两侧时间边界后汇总。"""
    segments = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                .order_by(RunSegment.seq).all())
    seg_ids = [s.id for s in segments]
    frags = (db.query(EnergyFragment)
             .filter(EnergyFragment.run_segment_id.in_(seg_ids)).all()) if seg_ids else []
    onb_frags = [f for f in frags if f.source == "ONBOARD"]
    sup_frags = [f for f in frags if f.source == "SUPPLY"]

    cov_onb = round(sum(f.coverage for f in onb_frags) / len(onb_frags), 4) if onb_frags else 0.0
    cov_sup = round(sum(f.coverage for f in sup_frags) / len(sup_frags), 4) if sup_frags else 0.0

    onboard_net = band_sum([_net_onboard(f.quantities) for f in onb_frags]) if onb_frags else UNKNOWN
    supply_net = band_sum([_net_supply(f.quantities) for f in sup_frags]) if sup_frags else UNKNOWN

    undetermined = []
    if cov_sup < COVERAGE_THRESHOLD:
        undetermined.append({"code": "SUPPLY_COVERAGE_INSUFFICIENT",
                             "detail": f"供电侧时间覆盖率 {cov_sup:.0%} < {COVERAGE_THRESHOLD:.0%},"
                                       " 供电净受电为未知范围"})
        supply_net = UNKNOWN  # 不以车载值冒充闭合计量
    if cov_onb < COVERAGE_THRESHOLD:
        undetermined.append({"code": "ONBOARD_COVERAGE_INSUFFICIENT",
                             "detail": f"车载侧时间覆盖率 {cov_onb:.0%}"})
    for f in onb_frags:
        for r in (f.detail or {}).get("resets", []):
            undetermined.append({"code": "DEVICE_RESET",
                                 "detail": f"片段{f.run_segment_id} 于 {r['at']} 重启,"
                                           f" 空窗 {r['gap_minutes']}min 消耗不可知"})

    diff = band_sub(supply_net, onboard_net)
    comparable = ("FULL" if cov_sup >= COVERAGE_THRESHOLD and cov_onb >= COVERAGE_THRESHOLD
                  else "PARTIAL" if cov_sup > 0 and cov_onb > 0 else "NONE")
    attribution = _attribute(db, run, [f.quantities for f in onb_frags],
                             [f.quantities for f in sup_frags], diff)

    rec = Reconciliation(
        run_id=run.id, run_version=run.version, method="RUN_ALIGNED",
        range_start=run.started_at, range_end=run.ended_at, comparable=comparable,
        coverage_onboard=cov_onb, coverage_supply=cov_sup,
        onboard_net=onboard_net, supply_net=supply_net, diff=diff,
        attribution=attribution, undetermined=undetermined)
    db.add(rec)
    db.flush()
    return rec


def reconcile_grid_aligned(db: Session, run: Run) -> Reconciliation:
    """路径B:运行映射进 区段×时间桶 网格,跨格部分以区间表示。"""
    cells = db.query(GridCell).order_by(GridCell.bucket_start).all()
    mine = [c for c in cells if run.id in (c.run_ids or [])]
    if not mine:
        rec = Reconciliation(
            run_id=run.id, run_version=run.version, method="GRID_ALIGNED",
            range_start=run.started_at, range_end=run.ended_at, comparable="NONE",
            coverage_onboard=0.0, coverage_supply=0.0,
            onboard_net=UNKNOWN, supply_net=UNKNOWN, diff=UNKNOWN,
            attribution={"line_loss": 0, "regen_not_returned": 0,
                         "metering_delay": 0, "unknown": 0, "assumptions": {}},
            undetermined=[{"code": "NO_GRID_CELLS", "detail": "运行未映射到任何网格"}])
        db.add(rec)
        db.flush()
        return rec

    onboard_net = band_sum([_net_onboard(c.onboard) for c in mine])
    supply_net = band_sum([_net_supply(c.supply) for c in mine])
    cov_onb = round(sum(1 for c in mine if c.onboard.get("traction", {}).get("v") is not None)
                    / len(mine), 4)
    cov_sup = round(sum(1 for c in mine if c.supply.get("import", {}).get("v") is not None)
                    / len(mine), 4)

    undetermined = []
    if cov_sup < 1.0:
        undetermined.append({"code": "SUPPLY_COVERAGE_INSUFFICIENT",
                             "detail": f"{len(mine)} 个网格中 {sum(1 for c in mine if c.supply.get('import', {}).get('v') is None)} 个供电侧未知"})
        supply_net = UNKNOWN
    # 跨格边界:首末桶为部分覆盖,差异区间天然更宽
    undetermined.append({"code": "GRID_EDGE_APPORTIONED",
                         "detail": "首末时间桶按时间比例分摊,跨格能量以区间表示"})

    diff = band_sub(supply_net, onboard_net)
    comparable = ("FULL" if cov_sup == 1.0 and cov_onb == 1.0
                  else "PARTIAL" if cov_sup > 0 and cov_onb > 0 else "NONE")
    attribution = _attribute(db, run, [c.onboard for c in mine],
                             [c.supply for c in mine], diff)

    rec = Reconciliation(
        run_id=run.id, run_version=run.version, method="GRID_ALIGNED",
        range_start=mine[0].bucket_start,
        range_end=mine[-1].bucket_start, comparable=comparable,
        coverage_onboard=cov_onb, coverage_supply=cov_sup,
        onboard_net=onboard_net, supply_net=supply_net, diff=diff,
        attribution=attribution, undetermined=undetermined)
    db.add(rec)
    db.flush()
    return rec
