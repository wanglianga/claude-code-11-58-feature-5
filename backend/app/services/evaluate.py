"""片段求值与网格累计。

- 路径A(运行对齐):对每个运行片段分别对齐车载与供电侧时间边界,再汇总为区段/运行能量。
- 路径B(网格对齐):先在固定 区段×时间桶 网格内累计两侧能量,再把运行映射进网格;
  列车跨格或短时回馈时以区间(lo/hi)表示。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..config import (COVERAGE_THRESHOLD, GRID_BUCKET_MINUTES,
                      MAX_AUX_RATE_KWH_PER_MIN, MAX_TRACTION_RATE_KWH_PER_MIN)
from ..models import (Device, EnergyFragment, GridCell, MeterReading, Run,
                      RunSegment, Section)
from .energy import band_sum, diff_counter
from .segmentation import segment_run


def _readings(db: Session, device_id: int, start: datetime, end: datetime):
    return (db.query(MeterReading)
            .filter(MeterReading.device_id == device_id,
                    MeterReading.sampled_at >= start,
                    MeterReading.sampled_at <= end)
            .order_by(MeterReading.sampled_at, MeterReading.seq).all())


def _onboard_device(db: Session, run: Run) -> Device | None:
    return (db.query(Device).filter(Device.kind == "ONBOARD",
                                    Device.train_id == run.train_id)
            .order_by(Device.id).first())


def evaluate_run(db: Session, run: Run) -> list[EnergyFragment]:
    """路径A:重切分并对每个片段生成车载/供电两侧能量片段(幂等)。"""
    segments = segment_run(db, run)
    seg_ids = [s.id for s in segments]
    if seg_ids:
        db.query(EnergyFragment).filter(EnergyFragment.run_segment_id.in_(seg_ids)).delete()

    device = _onboard_device(db, run)
    frags: list[EnergyFragment] = []
    for seg in segments:
        # ---- 车载侧 ----
        if device:
            rows = _readings(db, device.id, seg.start_ts, seg.end_ts)
            traction = diff_counter(rows, "traction_cum", seg.start_ts, seg.end_ts,
                                    MAX_TRACTION_RATE_KWH_PER_MIN)
            regen = diff_counter(rows, "regen_cum", seg.start_ts, seg.end_ts,
                                 MAX_TRACTION_RATE_KWH_PER_MIN)
            aux = diff_counter(rows, "aux_cum", seg.start_ts, seg.end_ts,
                               MAX_AUX_RATE_KWH_PER_MIN)
            coverage = min(traction.coverage, regen.coverage, aux.coverage)
            quality = ("GOOD" if coverage >= COVERAGE_THRESHOLD and not traction.resets
                       else "ESTIMATED" if coverage > 0 else "UNKNOWN")
            frags.append(EnergyFragment(
                run_segment_id=seg.id, source="ONBOARD",
                quantities={"traction": traction.band(), "regen": regen.band(),
                            "aux": aux.band()},
                quality=quality, coverage=round(coverage, 4),
                detail={"resets": traction.resets + regen.resets + aux.resets,
                        "multiplier_changed": traction.multiplier_changed,
                        "device_serial": device.serial}))
        # ---- 供电侧(区段变电所) ----
        section = db.get(Section, seg.section_id)
        if section and section.substation_device_id:
            rows = _readings(db, section.substation_device_id, seg.start_ts, seg.end_ts)
            imp = diff_counter(rows, "import_cum", seg.start_ts, seg.end_ts,
                               MAX_TRACTION_RATE_KWH_PER_MIN * 4)
            exp = diff_counter(rows, "export_cum", seg.start_ts, seg.end_ts,
                               MAX_TRACTION_RATE_KWH_PER_MIN * 4)
            coverage = min(imp.coverage, exp.coverage)
            # 供电表缺失或时间覆盖不足:输出未知范围,不以车载值冒充闭合计量
            if coverage < COVERAGE_THRESHOLD:
                quality = "UNKNOWN" if coverage == 0 else "ESTIMATED"
                quantities = {"import": {"v": None, "lo": None, "hi": None},
                              "export": {"v": None, "lo": None, "hi": None}}
            else:
                quality = "GOOD"
                quantities = {"import": imp.band(), "export": exp.band()}
            frags.append(EnergyFragment(
                run_segment_id=seg.id, source="SUPPLY", quantities=quantities,
                quality=quality, coverage=round(coverage, 4),
                detail={"substation_device_id": section.substation_device_id,
                        "coverage_threshold": COVERAGE_THRESHOLD,
                        "note": "coverage below threshold -> unknown band"
                        if coverage < COVERAGE_THRESHOLD else ""}))
    for f in frags:
        db.add(f)
    db.flush()
    return frags


def rebuild_grid(db: Session) -> list[GridCell]:
    """路径B:全量重建 区段×时间桶 网格。

    只取每个 run_key 的最新版本运行,避免迟到补传产生的新旧版本重复累计;
    列车跨格或短时回馈按时间比例分摊,以区间(lo/hi)表示。
    """
    db.query(GridCell).delete()
    latest: dict[str, Run] = {}
    for r in db.query(Run).order_by(Run.version).all():
        latest[r.run_key] = r

    def zero(keys):
        return {k: {"v": 0.0, "lo": 0.0, "hi": 0.0} for k in keys}

    def add_band(dst: dict, src: dict, ratio: float):
        if dst["v"] is None or src["v"] is None:
            dst.update({"v": None, "lo": None, "hi": None})
            return
        dst["v"] = round(dst["v"] + src["v"] * ratio, 3)
        dst["lo"] = round(dst["lo"] + src["lo"] * ratio, 3)
        dst["hi"] = None if dst["hi"] is None or src["hi"] is None \
            else round(dst["hi"] + src["hi"] * ratio, 3)

    acc: dict[tuple, dict] = {}
    bucket = timedelta(minutes=GRID_BUCKET_MINUTES)
    for run in latest.values():
        segments = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                    .order_by(RunSegment.seq).all())
        for seg in segments:
            frags = (db.query(EnergyFragment)
                     .filter(EnergyFragment.run_segment_id == seg.id).all())
            onb = next((f for f in frags if f.source == "ONBOARD"), None)
            sup = next((f for f in frags if f.source == "SUPPLY"), None)
            seg_min = max((seg.end_ts - seg.start_ts).total_seconds() / 60.0, 1e-9)
            t = seg.start_ts
            while t < seg.end_ts:
                bucket_start = t.replace(
                    minute=(t.minute // GRID_BUCKET_MINUTES) * GRID_BUCKET_MINUTES,
                    second=0, microsecond=0)
                bucket_end = min(bucket_start + bucket, seg.end_ts)
                ratio = ((bucket_end - max(t, bucket_start)).total_seconds()
                         / 60.0) / seg_min
                key = (seg.section_id, bucket_start)
                cell = acc.setdefault(key, {
                    "onboard": zero(("traction", "regen", "aux")),
                    "supply": zero(("import", "export")), "run_ids": set()})
                if onb:
                    for k in ("traction", "regen", "aux"):
                        add_band(cell["onboard"][k], onb.quantities[k], ratio)
                if sup:
                    for k in ("import", "export"):
                        add_band(cell["supply"][k], sup.quantities[k], ratio)
                cell["run_ids"].add(run.id)
                t = bucket_end

    cells = []
    for (sec_id, bs), a in sorted(acc.items(), key=lambda x: (x[0][1], x[0][0])):
        cell = GridCell(section_id=sec_id, bucket_start=bs,
                        bucket_minutes=GRID_BUCKET_MINUTES,
                        onboard=a["onboard"], supply=a["supply"],
                        run_ids=sorted(a["run_ids"]))
        db.add(cell)
        cells.append(cell)
    db.flush()
    return cells
