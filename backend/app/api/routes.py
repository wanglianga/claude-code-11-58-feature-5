"""API 路由。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import LATE_ARRIVAL_SECONDS
from ..db import get_db
from ..models import (ConfigVersion, Consist, Device, EnergyFragment, GridCell,
                      Line, MeterReading, Metric, Reconciliation, Run, RunEvent,
                      RunSegment, Section, Train, User)
from ..services.evaluate import evaluate_run, rebuild_grid
from ..services.metrics import publish_run_metrics, reprocess_run
from ..services.reconcile import reconcile_grid_aligned, reconcile_run_aligned

router = APIRouter(prefix="/api")


# ---------------- 鉴权 ----------------

def current_user(authorization: str = Header(default=""),
                 db: Session = Depends(get_db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    user = db.query(User).filter(User.token == token).first() if token else None
    if not user:
        raise HTTPException(401, "未登录或令牌无效")
    return user


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = (db.query(User).filter(User.username == body.username,
                                  User.password == body.password).first())
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    return {"token": user.token, "username": user.username, "role": user.role}


# ---------------- 元数据 ----------------

@router.get("/meta/overview")
def overview(db: Session = Depends(get_db), _: User = Depends(current_user)):
    latest_runs = {}
    for r in db.query(Run).order_by(Run.run_key, Run.version.desc()).all():
        latest_runs.setdefault(r.run_key, r)
    runs = list(latest_runs.values())
    recs = db.query(Reconciliation).all()
    return {
        "lines": db.query(Line).count(),
        "sections": db.query(Section).count(),
        "trains": db.query(Train).count(),
        "devices": db.query(Device).count(),
        "readings": db.query(MeterReading).count(),
        "runs": len(runs),
        "run_versions": db.query(Run).count(),
        "segments": db.query(RunSegment).count(),
        "fragments": db.query(EnergyFragment).count(),
        "reconciliations": len(recs),
        "metrics": db.query(Metric).count(),
        "comparable_full": sum(1 for r in recs if r.comparable == "FULL"),
        "comparable_partial": sum(1 for r in recs if r.comparable == "PARTIAL"),
    }


@router.get("/meta/catalog")
def catalog(db: Session = Depends(get_db), _: User = Depends(current_user)):
    lines = db.query(Line).all()
    sections = db.query(Section).order_by(Section.line_id, Section.seq).all()
    trains = db.query(Train).all()
    devices = db.query(Device).all()
    consists = db.query(Consist).all()
    return {
        "lines": [{"id": l.id, "code": l.code, "name": l.name} for l in lines],
        "sections": [{"id": s.id, "line_id": s.line_id, "code": s.code, "name": s.name,
                      "seq": s.seq, "length_km": s.length_km,
                      "multiplier_zone": s.multiplier_zone,
                      "substation_device_id": s.substation_device_id} for s in sections],
        "trains": [{"id": t.id, "code": t.code, "vehicle_model": t.vehicle_model} for t in trains],
        "devices": [{"id": d.id, "serial": d.serial, "kind": d.kind,
                     "train_id": d.train_id, "station_name": d.station_name} for d in devices],
        "consists": [{"id": c.id, "train_id": c.train_id, "code": c.code,
                      "valid_from": c.valid_from.isoformat(),
                      "valid_to": c.valid_to.isoformat() if c.valid_to else None}
                     for c in consists],
    }


# ---------------- 采集接入 ----------------

class ReadingIn(BaseModel):
    device_serial: str
    sampled_at: datetime
    received_at: Optional[datetime] = None
    seq: int
    traction_cum: Optional[float] = None
    regen_cum: Optional[float] = None
    aux_cum: Optional[float] = None
    import_cum: Optional[float] = None
    export_cum: Optional[float] = None
    multiplier: float = 1.0
    comm_status: str = "OK"


@router.post("/readings")
def ingest_readings(body: list[ReadingIn], db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    """批量接入原始累计值。迟到采样若落在已发布运行上,自动生成新运行版本并重发指标。"""
    now = datetime.utcnow()
    inserted, late_runs = 0, {}
    for item in body:
        device = db.query(Device).filter(Device.serial == item.device_serial).first()
        if not device:
            raise HTTPException(400, f"未知设备序列号 {item.device_serial}")
        received = item.received_at or now
        is_late = (received - item.sampled_at).total_seconds() > LATE_ARRIVAL_SECONDS
        db.add(MeterReading(device_id=device.id, sampled_at=item.sampled_at,
                            received_at=received, seq=item.seq,
                            traction_cum=item.traction_cum, regen_cum=item.regen_cum,
                            aux_cum=item.aux_cum, import_cum=item.import_cum,
                            export_cum=item.export_cum, multiplier=item.multiplier,
                            comm_status=item.comm_status))
        inserted += 1
        if is_late and device.kind == "ONBOARD":
            runs = (db.query(Run)
                    .filter(Run.train_id == device.train_id,
                            Run.started_at <= item.sampled_at,
                            Run.ended_at >= item.sampled_at,
                            Run.status == "PUBLISHED").all())
            for r in runs:
                late_runs[r.run_key] = r
    db.flush()

    new_versions = []
    for r in late_runs.values():
        new_run = reprocess_run(db, r, note="迟到补传")
        evaluate_run(db, new_run)
        rebuild_grid(db)
        reconcile_run_aligned(db, new_run)
        reconcile_grid_aligned(db, new_run)
        publish_run_metrics(db, new_run, note="迟到补传触发新版本")
        new_versions.append({"run_key": new_run.run_key, "version": new_run.version})
    db.commit()
    return {"inserted": inserted, "late_new_versions": new_versions}


@router.get("/readings")
def list_readings(device_id: Optional[int] = None, limit: int = 200,
                  db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(MeterReading).order_by(MeterReading.sampled_at.desc(),
                                        MeterReading.seq.desc())
    if device_id:
        q = q.filter(MeterReading.device_id == device_id)
    rows = q.limit(limit).all()
    return [{"id": r.id, "device_id": r.device_id, "serial": r.device.serial,
             "sampled_at": r.sampled_at.isoformat(),
             "received_at": r.received_at.isoformat(), "seq": r.seq,
             "traction_cum": r.traction_cum, "regen_cum": r.regen_cum,
             "aux_cum": r.aux_cum, "import_cum": r.import_cum,
             "export_cum": r.export_cum, "multiplier": r.multiplier,
             "comm_status": r.comm_status,
             "late": (r.received_at - r.sampled_at).total_seconds() > LATE_ARRIVAL_SECONDS}
            for r in rows]


@router.get("/devices")
def list_devices(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": d.id, "serial": d.serial, "kind": d.kind, "train_id": d.train_id,
             "station_name": d.station_name} for d in db.query(Device).all()]


# ---------------- 运行与能量 ----------------

def _run_json(db: Session, r: Run, detail: bool = False):
    out = {"id": r.id, "run_key": r.run_key, "version": r.version,
           "train": r.train.code, "vehicle_model": r.train.vehicle_model,
           "line": r.line.code, "driver_mode": r.driver_mode,
           "started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat(),
           "distance_km": r.distance_km, "passengers": r.passengers,
           "shift": r.shift, "status": r.status,
           "supersedes_id": r.supersedes_id}
    if detail:
        segs = (db.query(RunSegment).filter(RunSegment.run_id == r.id)
                .order_by(RunSegment.seq).all())
        out["segments"] = []
        for s in segs:
            frags = (db.query(EnergyFragment)
                     .filter(EnergyFragment.run_segment_id == s.id).all())
            out["segments"].append({
                "id": s.id, "seq": s.seq, "section": s.section.code,
                "direction": s.direction, "start_ts": s.start_ts.isoformat(),
                "end_ts": s.end_ts.isoformat(), "cut_reason": s.cut_reason,
                "consist_id": s.consist_id,
                "fragments": [{"id": f.id, "source": f.source, "quality": f.quality,
                               "coverage": f.coverage, "quantities": f.quantities,
                               "detail": f.detail} for f in frags]})
        out["events"] = [{"ts": e.ts.isoformat(), "type": e.type,
                          "section_id": e.section_id, "direction": e.direction}
                         for e in db.query(RunEvent)
                         .filter(RunEvent.run_id == r.id).order_by(RunEvent.ts).all()]
    return out


@router.get("/runs")
def list_runs(all_versions: bool = False, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = db.query(Run).order_by(Run.run_key, Run.version.desc())
    rows = q.all()
    if not all_versions:
        seen, rows2 = set(), []
        for r in rows:
            if r.run_key not in seen:
                seen.add(r.run_key)
                rows2.append(r)
        rows = rows2
    return [_run_json(db, r) for r in rows]


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    return _run_json(db, run, detail=True)


@router.get("/runs/{run_id}/reconciliation")
def run_reconciliation(run_id: int, method: str = "RUN_ALIGNED",
                       db: Session = Depends(get_db), _: User = Depends(current_user)):
    rec = (db.query(Reconciliation)
           .filter(Reconciliation.run_id == run_id, Reconciliation.method == method)
           .order_by(Reconciliation.id.desc()).first())
    if not rec:
        raise HTTPException(404, "该运行尚无此方法的核对结论")
    return {"id": rec.id, "run_id": rec.run_id, "run_version": rec.run_version,
            "method": rec.method, "range_start": rec.range_start.isoformat(),
            "range_end": rec.range_end.isoformat(), "comparable": rec.comparable,
            "coverage_onboard": rec.coverage_onboard,
            "coverage_supply": rec.coverage_supply,
            "onboard_net": rec.onboard_net, "supply_net": rec.supply_net,
            "diff": rec.diff, "attribution": rec.attribution,
            "undetermined": rec.undetermined}


@router.get("/runs/{run_id}/grid")
def run_grid(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    cells = (db.query(GridCell).order_by(GridCell.bucket_start).all())
    mine = [c for c in cells if run_id in (c.run_ids or [])]
    return [{"id": c.id, "section": c.section.code, "bucket_start": c.bucket_start.isoformat(),
             "bucket_minutes": c.bucket_minutes, "onboard": c.onboard,
             "supply": c.supply, "run_ids": c.run_ids} for c in mine]


# ---------------- 指标与复盘 ----------------

@router.get("/metrics")
def list_metrics(scope: Optional[str] = None, scope_key: Optional[str] = None,
                 db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(Metric).order_by(Metric.published_at.desc(), Metric.id.desc())
    if scope:
        q = q.filter(Metric.scope == scope)
    if scope_key:
        q = q.filter(Metric.scope_key == scope_key)
    return [{"id": m.id, "scope": m.scope, "scope_key": m.scope_key, "name": m.name,
             "value": m.value, "coverage": m.coverage, "method": m.method,
             "run_version": m.run_version, "published_at": m.published_at.isoformat(),
             "supersedes_id": m.supersedes_id, "diff_note": m.diff_note,
             "source_run_ids": m.source_run_ids} for m in q.limit(500).all()]


@router.get("/metrics/{metric_id}/replay")
def metric_replay(metric_id: int, db: Session = Depends(get_db),
                  _: User = Depends(current_user)):
    """复盘:某指标由哪些采样片段、配置版本和核对结论构成。"""
    m = db.get(Metric, metric_id)
    if not m:
        raise HTTPException(404, "指标不存在")
    runs = [db.get(Run, rid) for rid in m.source_run_ids]
    runs = [r for r in runs if r]
    chain = []
    cur = m
    while cur and cur.supersedes_id:
        prev = db.get(Metric, cur.supersedes_id)
        if not prev:
            break
        chain.append({"id": prev.id, "value": prev.value,
                      "run_version": prev.run_version,
                      "published_at": prev.published_at.isoformat(),
                      "diff_note": prev.diff_note})
        cur = prev
    runs_json = [_run_json(db, r, detail=True) for r in runs]
    # 采样片段涉及的所有原始读数
    reading_ids = []
    for r in runs:
        devs = (db.query(Device)
                .filter((Device.train_id == r.train_id) | (Device.kind == "SUBSTATION")).all())
        for d in devs:
            rows = (db.query(MeterReading)
                    .filter(MeterReading.device_id == d.id,
                            MeterReading.sampled_at >= r.started_at,
                            MeterReading.sampled_at <= r.ended_at)
                    .order_by(MeterReading.sampled_at, MeterReading.seq).all())
            reading_ids.extend(x.id for x in rows)
    recs = (db.query(Reconciliation)
            .filter(Reconciliation.run_id.in_([r.id for r in runs])).all()) if runs else []
    configs = (db.query(ConfigVersion)
               .filter(ConfigVersion.valid_from <= (runs[0].ended_at if runs else datetime.utcnow()))
               .order_by(ConfigVersion.valid_from).all()) if runs else []
    return {
        "metric": {"id": m.id, "scope": m.scope, "scope_key": m.scope_key,
                   "name": m.name, "value": m.value, "coverage": m.coverage,
                   "method": m.method, "run_version": m.run_version,
                   "published_at": m.published_at.isoformat(),
                   "diff_note": m.diff_note},
        "history": chain,
        "runs": runs_json,
        "reading_ids": sorted(set(reading_ids)),
        "reconciliations": [
            {"id": x.id, "method": x.method, "comparable": x.comparable,
             "onboard_net": x.onboard_net, "supply_net": x.supply_net,
             "diff": x.diff, "attribution": x.attribution,
             "undetermined": x.undetermined} for x in recs],
        "config_versions": [
            {"id": c.id, "kind": c.kind, "payload": c.payload,
             "valid_from": c.valid_from.isoformat(),
             "valid_to": c.valid_to.isoformat() if c.valid_to else None,
             "note": c.note} for c in configs],
    }


@router.get("/config-versions")
def list_config_versions(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": c.id, "kind": c.kind, "payload": c.payload,
             "valid_from": c.valid_from.isoformat(),
             "valid_to": c.valid_to.isoformat() if c.valid_to else None,
             "note": c.note}
            for c in db.query(ConfigVersion).order_by(ConfigVersion.valid_from).all()]
