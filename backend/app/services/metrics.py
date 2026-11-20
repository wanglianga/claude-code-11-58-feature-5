"""指标服务与迟到数据版本化。

- 指标按 运行/区段/班次/车型 发布:单位里程能耗、单位载客能耗、回馈比例、净能耗。
- 迟到采样只生成新运行版本;已发布指标保留原水位,新指标带差异说明。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import (EnergyFragment, Metric, Reconciliation, Run, RunSegment,
                      Section, Train)


def _run_net_and_regen(db: Session, run: Run):
    """取运行对齐路径下最新核对的车载净能耗与回馈量。"""
    segs = (db.query(RunSegment).filter(RunSegment.run_id == run.id).all())
    ids = [s.id for s in segs]
    if not ids:
        return None, None, 0.0
    frags = (db.query(EnergyFragment)
             .filter(EnergyFragment.run_segment_id.in_(ids),
                     EnergyFragment.source == "ONBOARD").all())
    if not frags:
        return None, None, 0.0
    net = regen = 0.0
    cov = sum(f.coverage for f in frags) / len(frags)
    for f in frags:
        q = f.quantities
        if q["traction"]["v"] is None:
            return None, None, cov
        net += q["traction"]["v"] + q["aux"]["v"] - q["regen"]["v"]
        regen += q["regen"]["v"]
    return round(net, 3), round(regen, 3), round(cov, 4)


def _publish(db: Session, scope: str, scope_key: str, name: str, value,
             coverage: float, method: str, run_version: int,
             source_run_ids: list[int], note: str | None = None) -> Metric:
    prev = (db.query(Metric)
            .filter(Metric.scope == scope, Metric.scope_key == scope_key,
                    Metric.name == name)
            .order_by(Metric.published_at.desc(), Metric.id.desc()).first())
    diff_note = note
    if prev is not None:
        if prev.value is None or value is None:
            delta = "不可比(一侧未知)"
        else:
            d = round(value - prev.value, 4)
            delta = f"较上一版 {'+' if d >= 0 else ''}{d}"
        diff_note = (note + ";" if note else "") + delta
    m = Metric(scope=scope, scope_key=scope_key, name=name, value=value,
               coverage=coverage, method=method, run_version=run_version,
               supersedes_id=prev.id if prev else None, diff_note=diff_note,
               source_run_ids=source_run_ids)
    db.add(m)
    db.flush()
    return m


def publish_run_metrics(db: Session, run: Run, method: str = "RUN_ALIGNED",
                        note: str | None = None) -> list[Metric]:
    """发布某运行版本的指标,并联动 区段/班次/车型 聚合。"""
    net, regen, cov = _run_net_and_regen(db, run)
    train = db.get(Train, run.train_id)
    out: list[Metric] = []
    per_km = round(net / run.distance_km, 4) if net is not None and run.distance_km else None
    per_pax = round(net / run.passengers, 4) if net is not None and run.passengers else None
    regen_ratio = (round(regen / (net + regen), 4)
                   if net is not None and (net + regen) > 0 else None)

    for name, val in [("NET_KWH", net), ("KWH_PER_KM", per_km),
                      ("KWH_PER_PASSENGER", per_pax), ("REGEN_RATIO", regen_ratio)]:
        out.append(_publish(db, "RUN", run.run_key, name, val, cov, method,
                            run.version, [run.id], note))

    # 区段聚合:取该运行各片段所在区段,按最新已发布运行版本求均值
    segs = (db.query(RunSegment).filter(RunSegment.run_id == run.id).all())
    for sec_id in {s.section_id for s in segs}:
        sec = db.get(Section, sec_id)
        runs = (db.query(Run).join(RunSegment, RunSegment.run_id == Run.id)
                .filter(RunSegment.section_id == sec_id, Run.status == "PUBLISHED")
                .all())
        vals = []
        for r in {r.run_key: r for r in runs}.values():  # 每 run_key 取最新版本
            latest = (db.query(Run).filter(Run.run_key == r.run_key)
                      .order_by(Run.version.desc()).first())
            n, _, _ = _run_net_and_regen(db, latest)
            if n is not None and latest.distance_km:
                vals.append(n / latest.distance_km)
        if vals:
            out.append(_publish(db, "SECTION", sec.code, "KWH_PER_KM",
                                round(sum(vals) / len(vals), 4), cov, method,
                                run.version, [run.id], note))

    # 班次 / 车型聚合(同口径均值)
    for scope, key in [("SHIFT", run.shift), ("VEHICLE_MODEL", train.vehicle_model)]:
        if not key:
            continue
        q = (db.query(Run).join(Train, Run.train_id == Train.id)
             .filter(Run.status == "PUBLISHED"))
        q = q.filter(Run.shift == key) if scope == "SHIFT" \
            else q.filter(Train.vehicle_model == key)
        vals = []
        for r in {r.run_key: r for r in q.all()}.values():
            latest = (db.query(Run).filter(Run.run_key == r.run_key)
                      .order_by(Run.version.desc()).first())
            n, _, _ = _run_net_and_regen(db, latest)
            if n is not None and latest.distance_km:
                vals.append(n / latest.distance_km)
        if vals:
            out.append(_publish(db, scope, key, "KWH_PER_KM",
                                round(sum(vals) / len(vals), 4), cov, method,
                                run.version, [run.id], note))
    run.status = "PUBLISHED"
    db.flush()
    return out


def reprocess_run(db: Session, run: Run, note: str = "迟到补传") -> Run:
    """迟到采样:生成 run_key 相同、version+1 的新运行版本;旧版本与旧指标保留。"""
    latest = (db.query(Run).filter(Run.run_key == run.run_key)
              .order_by(Run.version.desc()).first())
    new = Run(run_key=latest.run_key, version=latest.version + 1,
              supersedes_id=latest.id, train_id=latest.train_id,
              line_id=latest.line_id, driver_mode=latest.driver_mode,
              started_at=latest.started_at, ended_at=latest.ended_at,
              distance_km=latest.distance_km, passengers=latest.passengers,
              shift=latest.shift, status="OPEN")
    db.add(new)
    db.flush()
    # 复制运行事件与编组有效期(迟到数据只影响读数求值)
    from ..models import RunConsist, RunEvent
    for ev in db.query(RunEvent).filter(RunEvent.run_id == latest.id).all():
        db.add(RunEvent(run_id=new.id, ts=ev.ts, type=ev.type,
                        section_id=ev.section_id, direction=ev.direction,
                        payload=ev.payload))
    for rc in db.query(RunConsist).filter(RunConsist.run_id == latest.id).all():
        db.add(RunConsist(run_id=new.id, consist_id=rc.consist_id,
                          valid_from=rc.valid_from, valid_to=rc.valid_to))
    db.flush()
    return new
