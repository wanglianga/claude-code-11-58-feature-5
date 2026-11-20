"""设备健康 API:健康档案、残差趋势、报警与维修回查。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (ComponentInstall, HealthAlert, HealthWatchConfig,
                      MaintenanceEvent, ResidualPoint, Run, RunCondition,
                      SensorCalibration, Train, TrendRun)
from ..services.health import (default_params, record_maintenance,
                               run_trend_batch, run_trend_for_install)
from .routes import current_user
from ..models import User

router = APIRouter(prefix="/api/health")


# ---------------- 健康档案 ----------------

def _install_json(db: Session, i: ComponentInstall):
    train = db.get(Train, i.train_id)
    return {"id": i.id, "train_id": i.train_id, "train": train.code if train else None,
            "vehicle_model": train.vehicle_model if train else None,
            "component_type": i.component_type, "serial": i.serial,
            "installed_at": i.installed_at.isoformat(),
            "removed_at": i.removed_at.isoformat() if i.removed_at else None,
            "active": i.removed_at is None, "note": i.note}


def _event_json(e: MaintenanceEvent, late_seconds: int = 3600):
    late = (e.recorded_at - e.occurred_at).total_seconds() > late_seconds
    return {"id": e.id, "train_id": e.train_id,
            "component_install_id": e.component_install_id,
            "type": e.type, "occurred_at": e.occurred_at.isoformat(),
            "recorded_at": e.recorded_at.isoformat(),
            "late": late, "description": e.description}


@router.get("/profile")
def health_profile(train_id: Optional[int] = None, db: Session = Depends(get_db),
                   _: User = Depends(current_user)):
    """健康档案总览:部件安装/更换区间、维修与故障时间、校准状态、关注层级。"""
    q_i = db.query(ComponentInstall).order_by(ComponentInstall.train_id,
                                              ComponentInstall.installed_at)
    q_e = db.query(MaintenanceEvent).order_by(MaintenanceEvent.occurred_at)
    if train_id:
        q_i = q_i.filter(ComponentInstall.train_id == train_id)
        q_e = q_e.filter(MaintenanceEvent.train_id == train_id)
    installs = q_i.all()
    events = q_e.all()
    cals = (db.query(SensorCalibration)
            .order_by(SensorCalibration.device_id,
                      SensorCalibration.valid_from).all())
    return {
        "watch_configs": [
            {"id": c.id, "level": c.level, "component_type": c.component_type,
             "enabled": bool(c.enabled), "params": c.params, "note": c.note}
            for c in db.query(HealthWatchConfig).all()],
        "installs": [_install_json(db, i) for i in installs],
        "maintenance": [_event_json(e) for e in events],
        "calibrations": [
            {"id": c.id, "device_id": c.device_id,
             "serial": c.device.serial if c.device else None,
             "status": c.status, "valid_from": c.valid_from.isoformat(),
             "valid_to": c.valid_to.isoformat() if c.valid_to else None,
             "note": c.note} for c in cals],
        "default_params": default_params(),
    }


@router.get("/components")
def list_components(train_id: Optional[int] = None,
                    db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    q = db.query(ComponentInstall).order_by(ComponentInstall.train_id,
                                            ComponentInstall.installed_at)
    if train_id:
        q = q.filter(ComponentInstall.train_id == train_id)
    return [_install_json(db, i) for i in q.all()]


@router.get("/maintenance")
def list_maintenance(train_id: Optional[int] = None,
                     db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    q = db.query(MaintenanceEvent).order_by(MaintenanceEvent.occurred_at)
    if train_id:
        q = q.filter(MaintenanceEvent.train_id == train_id)
    return [_event_json(e) for e in q.all()]


class MaintenanceIn(BaseModel):
    train_id: int
    type: str                      # FAULT|REPAIR|OVERHAUL
    occurred_at: datetime
    description: str = ""
    component_install_id: Optional[int] = None
    recorded_at: Optional[datetime] = None  # 缺省=现在;显式晚于发生时刻可模拟晚到


@router.post("/maintenance")
def create_maintenance(body: MaintenanceIn, db: Session = Depends(get_db),
                       _: User = Depends(current_user)):
    """登记维修/故障记录。晚到记录自动触发新一轮趋势运行(健康期重标),
    旧报警保留当时依据。"""
    if body.type not in ("FAULT", "REPAIR", "OVERHAUL"):
        raise HTTPException(400, "type 必须为 FAULT|REPAIR|OVERHAUL")
    if not db.get(Train, body.train_id):
        raise HTTPException(404, "列车不存在")
    ev, late, triggered = record_maintenance(
        db, body.train_id, body.type, body.occurred_at, body.description,
        component_install_id=body.component_install_id,
        recorded_at=body.recorded_at)
    db.commit()
    return {"event": _event_json(ev), "late": late,
            "triggered_trend_run_ids": triggered}


class ConditionIn(BaseModel):
    run_key: str
    ambient_temp_c: float
    weather: str = "CLEAR"
    speed_restriction: bool = False


@router.post("/conditions")
def upsert_condition(body: ConditionIn, db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    """登记/更新运行条件(作用于该 run_key 的最新版本)。
    条件缺失的运行此前不具备报警资格;补登后需重新触发趋势运行才生效。"""
    run = (db.query(Run).filter(Run.run_key == body.run_key)
           .order_by(Run.version.desc()).first())
    if not run:
        raise HTTPException(404, "运行不存在")
    cond = db.query(RunCondition).filter(RunCondition.run_id == run.id).first()
    if cond:
        cond.ambient_temp_c = body.ambient_temp_c
        cond.weather = body.weather
        cond.speed_restriction = 1 if body.speed_restriction else 0
        cond.source = "MANUAL"
        cond.recorded_at = datetime.utcnow()
    else:
        cond = RunCondition(run_id=run.id, ambient_temp_c=body.ambient_temp_c,
                            weather=body.weather,
                            speed_restriction=1 if body.speed_restriction else 0,
                            source="MANUAL")
        db.add(cond)
    db.commit()
    return {"run_key": body.run_key, "run_id": run.id, "condition": "saved"}


# ---------------- 趋势运行 ----------------

class TrendRunIn(BaseModel):
    component_install_id: Optional[int] = None  # 缺省 = 全部受关注安装周期
    as_of: Optional[datetime] = None            # 缺省 = 现在


@router.post("/trend-runs")
def create_trend_run(body: TrendRunIn, db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    """手动触发趋势运行(每次生成新行,历史趋势运行保留)。"""
    as_of = body.as_of or datetime.utcnow()
    if body.component_install_id:
        inst = db.get(ComponentInstall, body.component_install_id)
        if not inst:
            raise HTTPException(404, "部件安装周期不存在")
        trs = [run_trend_for_install(db, inst, as_of, "MANUAL")]
    else:
        trs = run_trend_batch(db, as_of, "MANUAL")
    db.commit()
    return {"trend_run_ids": [t.id for t in trs]}


def _trend_summary_json(db: Session, tr: TrendRun):
    inst = db.get(ComponentInstall, tr.component_install_id)
    train = db.get(Train, inst.train_id) if inst else None
    return {"id": tr.id, "component_install_id": tr.component_install_id,
            "train": train.code if train else None,
            "vehicle_model": train.vehicle_model if train else None,
            "component_type": inst.component_type if inst else None,
            "component_serial": inst.serial if inst else None,
            "install_period": {
                "from": inst.installed_at.isoformat() if inst else None,
                "to": inst.removed_at.isoformat() if inst and inst.removed_at else None},
            "as_of": tr.as_of.isoformat(), "trigger": tr.trigger,
            "summary": tr.summary, "created_at": tr.created_at.isoformat()}


@router.get("/trend-runs")
def list_trend_runs(component_install_id: Optional[int] = None,
                    db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    q = db.query(TrendRun).order_by(TrendRun.id.desc())
    if component_install_id:
        q = q.filter(TrendRun.component_install_id == component_install_id)
    return [_trend_summary_json(db, t) for t in q.limit(100).all()]


def _point_json(p: ResidualPoint):
    run = p.run
    return {"id": p.id, "run_id": p.run_id, "run_key": run.run_key if run else None,
            "run_version": run.version if run else None,
            "started_at": p.started_at.isoformat(),
            "actual_per_km": p.actual_per_km,
            "baseline_lo": p.baseline_lo, "baseline_mid": p.baseline_mid,
            "baseline_hi": p.baseline_hi,
            "baseline_level": p.baseline_level,
            "baseline_samples": p.baseline_samples,
            "residual_pct": p.residual_pct, "excess": bool(p.excess),
            "eligible": p.eligible, "eligibility_reasons": p.eligibility_reasons,
            "health_state": p.health_state,
            "condition": p.condition_snapshot, "energy": p.energy_snapshot}


@router.get("/trend-runs/{trend_run_id}")
def trend_run_detail(trend_run_id: int, db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    tr = db.get(TrendRun, trend_run_id)
    if not tr:
        raise HTTPException(404, "趋势运行不存在")
    points = (db.query(ResidualPoint)
              .filter(ResidualPoint.trend_run_id == tr.id)
              .order_by(ResidualPoint.started_at).all())
    alerts = (db.query(HealthAlert)
              .filter(HealthAlert.trend_run_id == tr.id).all())
    out = _trend_summary_json(db, tr)
    out["params"] = tr.params
    out["points"] = [_point_json(p) for p in points]
    out["alert_ids"] = [a.id for a in alerts]
    return out


# ---------------- 报警与维修回查 ----------------

def _linked_maintenance(db: Session, alert: HealthAlert):
    """维修回查:同一部件安装周期(或整车级)在报警窗口前后的维修/故障记录;
    故障/维修发生在超限窗口前 30 天至后 30 天内视为对报警的确认。"""
    from datetime import timedelta
    events = (db.query(MaintenanceEvent)
              .filter(MaintenanceEvent.train_id == alert.train_id,
                      MaintenanceEvent.component_install_id.in_(
                          [None, alert.component_install_id]))
              .order_by(MaintenanceEvent.occurred_at).all())
    out = []
    for e in events:
        confirms = (e.type in ("FAULT", "REPAIR")
                    and alert.first_excess_at - timedelta(days=30)
                    <= e.occurred_at
                    <= alert.last_excess_at + timedelta(days=30))
        out.append({**_event_json(e), "confirms": confirms})
    return out


def _alert_json(db: Session, a: HealthAlert, detail: bool = False):
    inst = db.get(ComponentInstall, a.component_install_id)
    train = db.get(Train, a.train_id)
    linked = _linked_maintenance(db, a)
    out = {"id": a.id, "alert_key": a.alert_key, "trend_run_id": a.trend_run_id,
           "train": train.code if train else None,
           "vehicle_model": train.vehicle_model if train else None,
           "component_type": inst.component_type if inst else None,
           "component_serial": inst.serial if inst else None,
           "component_install_id": a.component_install_id,
           "type": a.type, "eligibility": a.eligibility,
           "first_excess_at": a.first_excess_at.isoformat(),
           "last_excess_at": a.last_excess_at.isoformat(),
           "persist_runs": a.persist_runs,
           "max_residual_pct": a.max_residual_pct,
           "excess_kwh": a.excess_kwh,
           "created_at": a.created_at.isoformat(),
           "confirmed_by": [e["id"] for e in linked if e["confirms"]]}
    if detail:
        out["basis"] = a.basis  # 当时依据快照(晚到记录不改写)
        out["linked_maintenance"] = linked
        points = (db.query(ResidualPoint)
                  .filter(ResidualPoint.id.in_(a.point_ids))
                  .order_by(ResidualPoint.started_at).all())
        out["points"] = [_point_json(p) for p in points]
    return out


@router.get("/alerts")
def list_alerts(train_id: Optional[int] = None, db: Session = Depends(get_db),
                _: User = Depends(current_user)):
    q = db.query(HealthAlert).order_by(HealthAlert.id.desc())
    if train_id:
        q = q.filter(HealthAlert.train_id == train_id)
    return [_alert_json(db, a) for a in q.limit(200).all()]


@router.get("/alerts/{alert_id}")
def alert_detail(alert_id: int, db: Session = Depends(get_db),
                 _: User = Depends(current_user)):
    """报警详情:当时依据快照 + 支撑残差点(原始能耗/条件变量) + 维修回查。"""
    a = db.get(HealthAlert, alert_id)
    if not a:
        raise HTTPException(404, "报警不存在")
    return _alert_json(db, a, detail=True)
