"""设备健康:条件化残差趋势与报警资格。

路线选择:鲁棒分层基线(车队规模小、健康标签随维修记录逐步完善,
需要对新部件/少样本状态可解释):
- 基线在 车型×线路×温度带×载客带×运行约束 的已确认健康运行分层上,
  取中位数 ± K·MAD 生成预期能耗区间;样本不足时逐级放宽条件回退,
  并记录每个残差点实际使用的层级;所有层都不足时输出"无基线",
  该运行不具备报警资格(不默认为正常)。
- 健康期:已知故障前 N 天内的运行标记 EXCLUDED_PRE_FAULT —— 只参与评分、
  不参与基线训练,防止故障前隐性劣化污染基线;故障发生后(未修复)的
  运行标记 POST_FAULT,不再用于报警。
- 趋势运行:按部件安装周期执行,部件更换后新周期重新起算;汇总超额能耗
  残差、持续长度与覆盖不足区间。
- 报警:连续超限达到持续长度 -> 慢性劣化;孤立剧增 -> 突发。
  校准失效/条件缺失/能耗未知 -> 降低或取消报警资格,这些运行不默认为正常。
- 维修记录晚到:触发新一轮趋势运行并重标健康期;旧报警保留当时依据。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from ..config import (HEALTH_BAND_FLOOR_PCT, HEALTH_MAD_K,
                      HEALTH_MIN_BASELINE_SAMPLES, HEALTH_PERSIST_RUNS,
                      HEALTH_PRE_FAULT_EXCLUDE_DAYS, HEALTH_SUDDEN_PCT,
                      LATE_MAINTENANCE_SECONDS)
from ..models import (ComponentInstall, Device, EnergyFragment, HealthAlert,
                      HealthWatchConfig, MaintenanceEvent, ResidualPoint, Run,
                      RunCondition, RunSegment, SensorCalibration, Train,
                      TrendRun)

# 报警资格等级(差 -> 不可用于报警;降 -> 可支撑报警但报警整体降级)
ELIGIBLE, DOWNGRADED, INELIGIBLE = "ELIGIBLE", "DOWNGRADED", "INELIGIBLE"

# 健康标签
HEALTHY, EXCLUDED_PRE_FAULT, POST_FAULT = (
    "HEALTHY", "EXCLUDED_PRE_FAULT", "POST_FAULT")


def default_params() -> dict:
    return {
        "min_baseline_samples": HEALTH_MIN_BASELINE_SAMPLES,
        "persist_runs": HEALTH_PERSIST_RUNS,
        "sudden_pct": HEALTH_SUDDEN_PCT,
        "pre_fault_exclude_days": HEALTH_PRE_FAULT_EXCLUDE_DAYS,
        "band_floor_pct": HEALTH_BAND_FLOOR_PCT,
        "mad_k": HEALTH_MAD_K,
    }


# ---------------- 条件分箱 ----------------

def temp_band(celsius: float) -> str:
    if celsius < 10:
        return "COLD"
    if celsius <= 25:
        return "MILD"
    return "HOT"


def load_band(passengers: int) -> str:
    if passengers < 600:
        return "LOW"
    if passengers <= 1000:
        return "MID"
    return "HIGH"


def _cond_key(model: str, line_id: int, tb: str, lb: str, restriction: bool,
              level: int) -> tuple:
    """分层条件键:1 最细(含运行约束) .. 5 最粗(仅车型),样本不足逐级回退。"""
    if level == 1:
        return (model, line_id, tb, lb, bool(restriction))
    if level == 2:
        return (model, line_id, tb, lb)
    if level == 3:
        return (model, line_id, tb)
    if level == 4:
        return (model, line_id)
    return (model,)


# ---------------- 基础查询 ----------------

def latest_runs(db: Session) -> list[Run]:
    """每个 run_key 只取最新版本(迟到补传的新版本取代旧版本)。"""
    latest: dict[str, Run] = {}
    for r in db.query(Run).order_by(Run.version).all():
        latest[r.run_key] = r
    return list(latest.values())


def run_energy(db: Session, run: Run) -> dict:
    """运行的车载净能耗事实(牵引+辅助-回馈)及其质量,来自已求值片段。"""
    segs = db.query(RunSegment).filter(RunSegment.run_id == run.id).all()
    ids = [s.id for s in segs]
    frags = (db.query(EnergyFragment)
             .filter(EnergyFragment.run_segment_id.in_(ids),
                     EnergyFragment.source == "ONBOARD").all()) if ids else []
    rank = {"GOOD": 0, "ESTIMATED": 1, "UNKNOWN": 2}
    quality = "GOOD" if frags else "UNKNOWN"
    net = 0.0
    cov = 0.0
    for f in frags:
        if rank[f.quality] > rank[quality]:
            quality = f.quality
        cov += f.coverage
        q = f.quantities
        if q["traction"]["v"] is None:
            net = None
        elif net is not None:
            net += q["traction"]["v"] + q["aux"]["v"] - q["regen"]["v"]
    cov = round(cov / len(frags), 4) if frags else 0.0
    per_km = round(net / run.distance_km, 4) if net is not None and run.distance_km else None
    return {"net_kwh": round(net, 3) if net is not None else None,
            "per_km": per_km, "quality": quality, "coverage": cov,
            "distance_km": run.distance_km}


def calibration_at(db: Session, train_id: int, ts: datetime) -> str:
    """车载表在 ts 的校准状态;无记录视为 UNKNOWN(不默认正常)。"""
    device = (db.query(Device)
              .filter(Device.kind == "ONBOARD", Device.train_id == train_id)
              .order_by(Device.id).first())
    if not device:
        return "UNKNOWN"
    rows = (db.query(SensorCalibration)
            .filter(SensorCalibration.device_id == device.id,
                    SensorCalibration.valid_from <= ts)
            .order_by(SensorCalibration.valid_from.desc()).all())
    for r in rows:
        if r.valid_to is None or ts < r.valid_to:
            return r.status
    return "UNKNOWN"


def _known_events(db: Session, as_of: datetime) -> list[MaintenanceEvent]:
    """as_of 时刻已登记(known)的维修/故障记录。晚到记录在之前的分析中不可见。"""
    return (db.query(MaintenanceEvent)
            .filter(MaintenanceEvent.recorded_at <= as_of)
            .order_by(MaintenanceEvent.occurred_at).all())


def health_state(run_ts: datetime, install: ComponentInstall,
                 events: list[MaintenanceEvent], params: dict) -> str:
    """运行在指定安装周期下的健康标签(基于当前已知的维修记录)。"""
    rel = [e for e in events
           if e.train_id == install.train_id
           and (e.component_install_id in (None, install.id))]
    prior = [e for e in rel if e.occurred_at <= run_ts]
    if prior:
        latest = max(prior, key=lambda e: e.occurred_at)
        if latest.type == "FAULT":
            return POST_FAULT  # 故障未修复,不用于基线也不用于报警
    window = timedelta(days=params["pre_fault_exclude_days"])
    for e in rel:
        if e.type == "FAULT" and run_ts < e.occurred_at <= run_ts + window:
            return EXCLUDED_PRE_FAULT  # 故障前隐性劣化窗口:只评分,不进基线
    return HEALTHY


def watched_installs(db: Session) -> list[ComponentInstall]:
    """允许关注的设备层级闸门:仅启用的 (COMPONENT, 部件类型) 参与趋势分析。"""
    enabled = {c.component_type for c in db.query(HealthWatchConfig)
               .filter(HealthWatchConfig.level == "COMPONENT",
                       HealthWatchConfig.enabled == 1).all()}
    if not enabled:
        return []
    return (db.query(ComponentInstall)
            .filter(ComponentInstall.component_type.in_(enabled))
            .order_by(ComponentInstall.id).all())


def _install_at(installs: list[ComponentInstall], train_id: int,
                ts: datetime) -> ComponentInstall | None:
    for i in installs:
        if i.train_id != train_id:
            continue
        if i.installed_at <= ts and (i.removed_at is None or ts < i.removed_at):
            return i
    return None


# ---------------- 基线 ----------------

def _robust_band(values: list[float], params: dict):
    """中位数 ± K·MAD 的鲁棒区间,半宽下限为中位数的 band_floor_pct。"""
    mid = median(values)
    mad = median([abs(v - mid) for v in values])
    half = max(params["mad_k"] * mad * 1.4826, params["band_floor_pct"] * mid)
    return round(mid - half, 4), round(mid, 4), round(mid + half, 4)


def _build_pool(db: Session, installs: list[ComponentInstall],
                events: list[MaintenanceEvent], as_of: datetime,
                params: dict) -> dict:
    """基线训练池:仅已确认健康、校准有效、条件齐全、能耗质量良好的运行。

    返回 {level: {cond_key: [per_km, ...]}};同一运行可能进入多个层级的桶。
    """
    pool: dict[int, dict[tuple, list[float]]] = {lv: {} for lv in range(1, 6)}
    trains = {t.id: t for t in db.query(Train).all()}
    conds = {c.run_id: c for c in db.query(RunCondition).all()}
    for run in latest_runs(db):
        if run.started_at > as_of:
            continue
        inst = _install_at(installs, run.train_id, run.started_at)
        if inst is None:
            continue  # 不在任何受关注安装周期内
        if health_state(run.started_at, inst, events, params) != HEALTHY:
            continue  # 故障前窗口/故障后:只评分,不污染基线
        cond = conds.get(run.id)
        if cond is None:
            continue  # 条件缺失无法分层,不进基线
        if calibration_at(db, run.train_id, run.started_at) != "VALID":
            continue  # 校准不可信的读数不进基线
        energy = run_energy(db, run)
        if energy["quality"] != "GOOD" or energy["per_km"] is None:
            continue
        train = trains[run.train_id]
        tb, lb = temp_band(cond.ambient_temp_c), load_band(run.passengers)
        for lv in range(1, 6):
            key = _cond_key(train.vehicle_model, run.line_id, tb, lb,
                            cond.speed_restriction, lv)
            pool[lv].setdefault(key, []).append(energy["per_km"])
    return pool


def _baseline_for(pool: dict, model: str, line_id: int, tb: str, lb: str,
                  restriction: bool, params: dict):
    """按层级回退查找基线;返回 (lo, mid, hi, level, samples) 或 None。"""
    for lv in range(1, 6):
        key = _cond_key(model, line_id, tb, lb, restriction, lv)
        values = pool[lv].get(key, [])
        if len(values) >= params["min_baseline_samples"]:
            lo, mid, hi = _robust_band(values, params)
            return lo, mid, hi, lv, len(values)
    return None


# ---------------- 趋势运行 ----------------

def _score_run(db: Session, run: Run, install: ComponentInstall,
               events: list[MaintenanceEvent], pool: dict, params: dict,
               trains: dict, conds: dict) -> dict:
    """计算单个运行的残差点(不落库)。"""
    train = trains[run.train_id]
    cond = conds.get(run.id)
    energy = run_energy(db, run)
    calib = calibration_at(db, run.train_id, run.started_at)
    hstate = health_state(run.started_at, install, events, params)

    band = None
    if cond is not None and energy["per_km"] is not None:
        tb, lb = temp_band(cond.ambient_temp_c), load_band(run.passengers)
        band = _baseline_for(pool, train.vehicle_model, run.line_id, tb, lb,
                             cond.speed_restriction, params)

    # ---- 报警资格:取消类原因(不默认为正常,只标记不可判定) ----
    reasons = []
    if energy["per_km"] is None or energy["quality"] == "UNKNOWN":
        reasons.append("ENERGY_UNKNOWN")
    elif energy["quality"] == "ESTIMATED":
        reasons.append("ENERGY_ESTIMATED")  # 能耗本身是估计值,不足以定性设备
    if cond is None:
        reasons.append("CONDITION_MISSING")
    if calib in ("FAILED", "UNKNOWN"):
        reasons.append("CALIBRATION_FAILED" if calib == "FAILED"
                     else "CALIBRATION_UNKNOWN")
    if hstate == POST_FAULT:
        reasons.append("POST_FAULT")
    if band is None and cond is not None and energy["per_km"] is not None:
        reasons.append("NO_BASELINE")
    if reasons:
        eligible = INELIGIBLE
    else:
        # ---- 降级类原因:可支撑报警,但报警整体降级 ----
        if calib == "EXPIRED":
            reasons.append("CALIBRATION_EXPIRED")
        eligible = DOWNGRADED if reasons else ELIGIBLE

    lo = mid = hi = level = None
    samples = 0
    residual_pct = None
    excess = 0
    if band is not None:
        lo, mid, hi, level, samples = band
        if energy["per_km"] is not None and mid:
            residual_pct = round((energy["per_km"] - mid) / mid, 4)
            excess = 1 if energy["per_km"] > hi else 0

    return {
        "run": run, "started_at": run.started_at,
        "actual_per_km": energy["per_km"],
        "baseline_lo": lo, "baseline_mid": mid, "baseline_hi": hi,
        "baseline_level": level, "baseline_samples": samples,
        "residual_pct": residual_pct, "excess": excess,
        "eligible": eligible, "eligibility_reasons": reasons,
        "health_state": hstate,
        "condition_snapshot": (
            {"ambient_temp_c": cond.ambient_temp_c, "weather": cond.weather,
             "speed_restriction": bool(cond.speed_restriction),
             "temp_band": temp_band(cond.ambient_temp_c),
             "load_band": load_band(run.passengers),
             "passengers": run.passengers}
            if cond else None),
        "energy_snapshot": energy,
    }


def _merge_gaps(points: list[dict]) -> list[dict]:
    """覆盖不足区间:连续 INELIGIBLE 点合并为区间段,附带原因集合。"""
    gaps, cur = [], None
    for p in points:
        if p["eligible"] == INELIGIBLE:
            if cur is None:
                cur = {"from": p["started_at"], "to": p["started_at"],
                       "reasons": sorted(set(p["eligibility_reasons"]))}
            else:
                cur["to"] = p["started_at"]
                cur["reasons"] = sorted(set(cur["reasons"])
                                        | set(p["eligibility_reasons"]))
        else:
            if cur:
                gaps.append(cur)
                cur = None
    if cur:
        gaps.append(cur)
    for g in gaps:
        g["from"] = g["from"].isoformat()
        g["to"] = g["to"].isoformat()
    return gaps


def _emit_alerts(db: Session, tr: TrendRun, install: ComponentInstall,
                 points: list[dict], point_ids: list[int], params: dict,
                 as_of: datetime, trigger: str, events: list[MaintenanceEvent],
                 health_note: str):
    """由残差点链生成报警:连续超限->慢性劣化;孤立剧增->突发。
    同一安装周期内 (类型, 首次超限时刻) 相同的报警不重复创建。"""
    chains, cur = [], []
    for p, pid in zip(points, point_ids):
        if p["excess"] and p["eligible"] != INELIGIBLE:
            cur.append((p, pid))
        else:
            if cur:
                chains.append(cur)
                cur = []
    if cur:
        chains.append(cur)

    created = []
    for chain in chains:
        max_res = max(p["residual_pct"] for p, _ in chain)
        if len(chain) >= params["persist_runs"]:
            atype = "DEGRADATION"
        elif max_res >= params["sudden_pct"]:
            atype = "SUDDEN"
        else:
            continue  # 既不够持续也不够突发:不报警
        first, last = chain[0][0], chain[-1][0]
        dup = (db.query(HealthAlert)
               .filter(HealthAlert.component_install_id == install.id,
                       HealthAlert.type == atype,
                       HealthAlert.first_excess_at == first["started_at"])
               .first())
        if dup:
            continue  # 结论与既有报警一致:旧报警保留当时依据,不重复创建
        eligibility = "QUALIFIED"
        if any(p["eligible"] == DOWNGRADED for p, _ in chain):
            eligibility = "DOWNGRADED"
        excess_kwh = round(sum(
            max(0.0, (p["actual_per_km"] - p["baseline_mid"])
                * p["energy_snapshot"]["distance_km"]) for p, _ in chain), 2)
        n = db.query(HealthAlert).count() + len(created) + 1
        alert = HealthAlert(
            alert_key=f"ALT-{n:04d}", trend_run_id=tr.id,
            component_install_id=install.id, train_id=install.train_id,
            type=atype, eligibility=eligibility,
            first_excess_at=first["started_at"],
            last_excess_at=last["started_at"],
            persist_runs=len(chain), max_residual_pct=max_res,
            excess_kwh=excess_kwh,
            point_ids=[pid for _, pid in chain],
            basis={
                "as_of": as_of.isoformat(), "trigger": trigger,
                "params": params, "health_note": health_note,
                "known_events": [
                    {"id": e.id, "type": e.type,
                     "occurred_at": e.occurred_at.isoformat(),
                     "recorded_at": e.recorded_at.isoformat()}
                    for e in events
                    if e.train_id == install.train_id
                    and (e.component_install_id in (None, install.id))],
                "chain": [
                    {"run_key": p["run"].run_key, "run_id": p["run"].id,
                     "started_at": p["started_at"].isoformat(),
                     "actual_per_km": p["actual_per_km"],
                     "baseline_mid": p["baseline_mid"],
                     "baseline_hi": p["baseline_hi"],
                     "baseline_level": p["baseline_level"],
                     "residual_pct": p["residual_pct"],
                     "eligible": p["eligible"],
                     "health_state": p["health_state"]}
                    for p, _ in chain],
            })
        db.add(alert)
        created.append(alert)
    db.flush()
    return created


def run_trend_for_install(db: Session, install: ComponentInstall,
                          as_of: datetime, trigger: str,
                          pool: dict | None = None,
                          events: list | None = None,
                          params: dict | None = None) -> TrendRun:
    """对一个部件安装周期执行一次完整残差趋势分析(每次生成新趋势运行)。"""
    params = params or default_params()
    installs = watched_installs(db)
    events = events if events is not None else _known_events(db, as_of)
    pool = pool if pool is not None else _build_pool(db, installs, events,
                                                     as_of, params)
    trains = {t.id: t for t in db.query(Train).all()}
    conds = {c.run_id: c for c in db.query(RunCondition).all()}

    runs = [r for r in latest_runs(db)
            if r.train_id == install.train_id
            and install.installed_at <= r.started_at
            and (install.removed_at is None or r.started_at < install.removed_at)
            and r.started_at <= as_of]
    runs.sort(key=lambda r: r.started_at)

    points = [_score_run(db, r, install, events, pool, params, trains, conds)
              for r in runs]

    known_faults = [e for e in events
                    if e.train_id == install.train_id and e.type == "FAULT"
                    and (e.component_install_id in (None, install.id))]
    health_note = (f"已知故障{len(known_faults)}起,故障前"
                   f"{params['pre_fault_exclude_days']}天窗口已排除出基线训练"
                   if known_faults else
                   "无已知故障,安装周期内全部健康期参与基线")

    n_excess = sum(1 for p in points if p["excess"])
    suppressed = sum(1 for p in points
                     if p["excess"] and p["eligible"] == INELIGIBLE)
    # 持续长度:连续超限(具备报警资格)最长链
    max_persist = cur = 0
    for p in points:
        if p["excess"] and p["eligible"] != INELIGIBLE:
            cur += 1
            max_persist = max(max_persist, cur)
        else:
            cur = 0
    excess_kwh_total = round(sum(
        max(0.0, (p["actual_per_km"] - p["baseline_mid"])
            * p["energy_snapshot"]["distance_km"])
        for p in points
        if p["excess"] and p["eligible"] != INELIGIBLE), 2)

    tr = TrendRun(
        component_install_id=install.id, as_of=as_of, trigger=trigger,
        params=params,
        summary={
            "n_points": len(points),
            "n_eligible": sum(1 for p in points if p["eligible"] == ELIGIBLE),
            "n_downgraded": sum(1 for p in points if p["eligible"] == DOWNGRADED),
            "n_ineligible": sum(1 for p in points if p["eligible"] == INELIGIBLE),
            "n_excess": n_excess,
            "suppressed_excess": suppressed,
            "excess_kwh": excess_kwh_total,
            "max_persist": max_persist,
            "coverage_gaps": _merge_gaps(points),
            "health_note": health_note,
        })
    db.add(tr)
    db.flush()

    point_ids = []
    for p in points:
        rp = ResidualPoint(
            trend_run_id=tr.id, run_id=p["run"].id,
            started_at=p["started_at"], actual_per_km=p["actual_per_km"],
            baseline_lo=p["baseline_lo"], baseline_mid=p["baseline_mid"],
            baseline_hi=p["baseline_hi"], baseline_level=p["baseline_level"],
            baseline_samples=p["baseline_samples"],
            residual_pct=p["residual_pct"], excess=p["excess"],
            eligible=p["eligible"],
            eligibility_reasons=p["eligibility_reasons"],
            health_state=p["health_state"],
            condition_snapshot=p["condition_snapshot"],
            energy_snapshot=p["energy_snapshot"])
        db.add(rp)
        db.flush()
        point_ids.append(rp.id)

    _emit_alerts(db, tr, install, points, point_ids, params, as_of, trigger,
                 events, health_note)
    db.flush()
    return tr


def run_trend_batch(db: Session, as_of: datetime, trigger: str) -> list[TrendRun]:
    """对所有受关注安装周期执行一轮趋势运行(共享同一基线训练池)。"""
    params = default_params()
    installs = watched_installs(db)
    events = _known_events(db, as_of)
    pool = _build_pool(db, installs, events, as_of, params)
    return [run_trend_for_install(db, inst, as_of, trigger, pool=pool,
                                  events=events, params=params)
            for inst in installs]


def record_maintenance(db: Session, train_id: int, type_: str,
                       occurred_at: datetime, description: str,
                       component_install_id: int | None = None,
                       recorded_at: datetime | None = None):
    """登记检修/故障记录。晚到记录触发新一轮趋势运行并重标健康期;
    已产生的旧报警保留当时依据,不被改写。"""
    recorded_at = recorded_at or datetime.utcnow()
    ev = MaintenanceEvent(train_id=train_id, type=type_,
                          occurred_at=occurred_at, recorded_at=recorded_at,
                          description=description,
                          component_install_id=component_install_id)
    db.add(ev)
    db.flush()
    late = (recorded_at - occurred_at).total_seconds() > LATE_MAINTENANCE_SECONDS
    triggered = []
    if late:
        # 健康标签发生变化:全量重分析(本车故障窗口也会影响他车基线池)
        for tr in run_trend_batch(db, recorded_at, "LATE_MAINTENANCE"):
            triggered.append(tr.id)
    return ev, late, triggered
