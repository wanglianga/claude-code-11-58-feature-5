"""种子数据:五类验收场景 + 设备健康六类验收场景。

RUN-1001 跨区段折返(T01, L1, S1->S2 上行折返后下行返回)
RUN-1002 设备重启 + 倍率区间切换 + 运行中换编组(T02, L1)
RUN-1003 回馈受限(T03, L2/S3,变电所不接受回馈上网)
RUN-1004 车载与供电表差异(T01, L1,差异=线损+未明残差)
RUN-1005 补传读数(T02, L1,先按缺测发布 v1,迟到数据触发 v2)
RUN-1006 供电表缺失(T03, L2,供电侧输出未知范围,不以车载冒充闭合)

设备健康(RUN-2xxx 历史运行 + 健康档案):
季节负荷变化  T01 八月高温运行绝对能耗高,但落在高温条件层基线内 -> 不报警
部件更换      T02 牵引电机 10-10 更换 -> 新安装周期趋势重新起算
慢性劣化      T01 牵引电机 11 月起残差持续超限 -> 慢性劣化报警
突发故障      T03 10-22 单点剧增 -> 突发报警(当日故障记录确认)
校准失效      OB-T02 校准过期/失效、OB-T03 校准过期 -> 报警资格降低/取消,不默认正常
维修记录迟到  T01 故障 11-15 发生、11-25 才登记 -> 健康期重标,旧报警保留当时依据
"""
from __future__ import annotations

import zlib
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (ComponentInstall, ConfigVersion, Consist, Device,
                     HealthWatchConfig, Line, MaintenanceEvent, MeterReading,
                     Run, RunCondition, RunConsist, RunEvent, Section,
                     SensorCalibration, Train, User, create_all, drop_all)
from .services.evaluate import evaluate_run, rebuild_grid
from .services.health import record_maintenance, run_trend_batch
from .services.metrics import publish_run_metrics, reprocess_run
from .services.reconcile import reconcile_grid_aligned, reconcile_run_aligned

D1 = datetime(2026, 10, 20, 8, 0)   # 场景1/2
D4 = datetime(2026, 10, 21, 14, 0)  # 场景4
D5 = datetime(2026, 10, 22, 16, 0)  # 场景5
D3 = datetime(2026, 11, 18, 7, 0)   # 场景3(回馈受限配置生效后)
D6 = datetime(2026, 11, 19, 9, 0)   # 场景6

STEP = timedelta(minutes=5)


class Counter:
    """每台设备的累计表码状态(跨运行连续累计)。"""
    def __init__(self):
        self.v = {"traction": 1000.0, "regen": 200.0, "aux": 500.0,
                  "import": 5000.0, "export": 300.0}
        self.seq = 0


def gen_onboard(db: Session, device: Device, st: Counter, t0, t1,
                rates=(0.5, 0.1, 0.05), multiplier=1.0,
                reset_at=None, mult_change_at=None, mult_after=2.0,
                skip_after=None, received_delay_min=0):
    """生成车载读数。reset_at: 该时刻设备重启,计数器归零;
    mult_change_at: 倍率切换时刻;skip_after: 该时刻后缺测(通信中断)。"""
    rows = []
    t, mult = t0, multiplier
    while t <= t1:
        if skip_after and t > skip_after:
            break
        if mult_change_at and t >= mult_change_at:
            mult = mult_after
        if reset_at and t == reset_at:
            st.v["traction"] = st.v["regen"] = st.v["aux"] = 0.0
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t,
            received_at=t + timedelta(minutes=received_delay_min),
            seq=st.seq, traction_cum=round(st.v["traction"], 3),
            regen_cum=round(st.v["regen"], 3), aux_cum=round(st.v["aux"], 3),
            multiplier=mult, comm_status="OK"))
        st.v["traction"] += rates[0] * 5
        st.v["regen"] += rates[1] * 5
        st.v["aux"] += rates[2] * 5
        t += STEP
    db.add_all(rows)
    return rows


def gen_supply(db: Session, device: Device, st: Counter, t0, t1,
               imp_rate=0.62, exp_rate=0.12):
    rows, t = [], t0
    while t <= t1:
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t, received_at=t + timedelta(seconds=30),
            seq=st.seq, import_cum=round(st.v["import"], 3),
            export_cum=round(st.v["export"], 3), multiplier=1.0, comm_status="OK"))
        st.v["import"] += imp_rate * 5
        st.v["export"] += exp_rate * 5
        t += STEP
    db.add_all(rows)
    return rows


def mk_run(db, key, train, line, start, end, dist, pax, shift, mode="AM"):
    run = Run(run_key=key, version=1, train_id=train.id, line_id=line.id,
              driver_mode=mode, started_at=start, ended_at=end,
              distance_km=dist, passengers=pax, shift=shift)
    db.add(run)
    db.flush()
    return run


def ev(db, run, ts, type_, section=None, direction=None, payload=None):
    db.add(RunEvent(run_id=run.id, ts=ts, type=type_,
                    section_id=section.id if section else None,
                    direction=direction, payload=payload))


def process(db, run, note=None):
    db.flush()  # 会话关闭 autoflush,先落库再切分求值
    evaluate_run(db, run)
    rebuild_grid(db)
    reconcile_run_aligned(db, run)
    reconcile_grid_aligned(db, run)
    publish_run_metrics(db, run, note=note)


# ---------------- 设备健康场景 ----------------

HEALTH_BASE_PER_KM = {"M1": 0.82, "M2": 1.40}  # 各车型健康状态单位净能耗(kWh/km)


def _jitter(run_key: str) -> float:
    """确定性 ±1% 抖动(同一 run_key 跨次重建结果一致)。"""
    return 1.0 + ((zlib.crc32(run_key.encode()) % 7) - 3) / 300.0


def gen_health_run(db: Session, st: dict, run_key: str, train: Train,
                   line: Line, section: Section, sub: Device, consist: Consist,
                   ob: Device, start: datetime, temp: float, pax: int,
                   factor: float, restriction: bool = False,
                   with_condition: bool = True, shift: str = "平峰-健康"):
    """生成一趟 20 分钟单区段运行:单位净能耗 = 车型基值 × factor × 抖动。

    factor 已含温度/载客/限速/劣化等全部放大因素;车载读数按
    牵引:回馈:辅助 = 1.30 : 0.42 : 0.12 拆分(净 = 牵引 + 辅助 - 回馈)。
    """
    dur = timedelta(minutes=20)
    dist = section.length_km
    net = HEALTH_BASE_PER_KM[train.vehicle_model] * factor * _jitter(run_key) * dist
    traction_t, regen_t, aux_t = net * 1.30, net * 0.42, net * 0.12
    run = mk_run(db, run_key, train, line, start, start + dur, dist, pax, shift)
    db.add(RunConsist(run_id=run.id, consist_id=consist.id, valid_from=start))
    ev(db, run, start, "ENTER_SECTION", section, "UP")
    ev(db, run, start + dur, "EXIT_SECTION", section, "UP")
    gen_onboard(db, ob, st[ob.serial], start, start + dur,
                rates=(traction_t / 20, regen_t / 20, aux_t / 20))
    gen_supply(db, sub, st[sub.serial], start, start + dur + timedelta(minutes=5),
               imp_rate=net * 1.06 / 20, exp_rate=regen_t * 0.9 / 20)
    if with_condition:
        db.add(RunCondition(run_id=run.id, ambient_temp_c=temp, weather="CLEAR",
                            speed_restriction=1 if restriction else 0))
    process(db, run)
    return run


def seed_health(db: Session, st: dict, t01, t02, t03, l1, l2, s1, s3,
                sub1, sub3, c01, c02a, c02b, c03, ob1, ob2, ob3):
    """健康档案 + 六类验收场景的历史运行,最后执行两批趋势运行。"""
    t0 = datetime(2026, 1, 1)
    # ---- 允许关注的设备层级 ----
    db.add_all([
        HealthWatchConfig(level="COMPONENT", component_type="TRACTION_MOTOR",
                          enabled=1, params={},
                          note="牵引电机:启用部件级条件化残差趋势"),
        HealthWatchConfig(level="TRAIN", component_type=None, enabled=0,
                          params={},
                          note="整车级未启用:车队规模小,部件级已覆盖"),
    ])
    # ---- 部件安装/更换区间 ----
    i_t01 = ComponentInstall(train_id=t01.id, component_type="TRACTION_MOTOR",
                             serial="TM-T01-A", installed_at=t0, note="在役")
    i_t02a = ComponentInstall(train_id=t02.id, component_type="TRACTION_MOTOR",
                              serial="TM-T02-A", installed_at=t0,
                              removed_at=datetime(2026, 10, 10, 9),
                              note="到达寿命,更换退役")
    i_t02b = ComponentInstall(train_id=t02.id, component_type="TRACTION_MOTOR",
                              serial="TM-T02-B",
                              installed_at=datetime(2026, 10, 10, 9), note="在役")
    i_t03a = ComponentInstall(train_id=t03.id, component_type="TRACTION_MOTOR",
                              serial="TM-T03-A", installed_at=t0,
                              removed_at=datetime(2026, 10, 23, 14),
                              note="接地故障后更换退役")
    i_t03b = ComponentInstall(train_id=t03.id, component_type="TRACTION_MOTOR",
                              serial="TM-T03-B",
                              installed_at=datetime(2026, 10, 23, 14), note="在役")
    db.add_all([i_t01, i_t02a, i_t02b, i_t03a, i_t03b])
    db.flush()
    # ---- 传感器校准状态 ----
    db.add_all([
        SensorCalibration(device_id=ob1.id, status="VALID", valid_from=t0),
        SensorCalibration(device_id=ob2.id, status="VALID", valid_from=t0,
                          valid_to=datetime(2026, 10, 8)),
        SensorCalibration(device_id=ob2.id, status="EXPIRED",
                          valid_from=datetime(2026, 10, 8),
                          valid_to=datetime(2026, 10, 20), note="校准到期未检"),
        SensorCalibration(device_id=ob2.id, status="VALID",
                          valid_from=datetime(2026, 10, 20),
                          valid_to=datetime(2026, 11, 1), note="临时校验合格"),
        SensorCalibration(device_id=ob2.id, status="FAILED",
                          valid_from=datetime(2026, 11, 1),
                          valid_to=datetime(2026, 11, 6),
                          note="校验发现超差,期间读数不可信"),
        SensorCalibration(device_id=ob2.id, status="VALID",
                          valid_from=datetime(2026, 11, 6), note="重新校准合格"),
        SensorCalibration(device_id=ob3.id, status="VALID", valid_from=t0,
                          valid_to=datetime(2026, 10, 28)),
        SensorCalibration(device_id=ob3.id, status="EXPIRED",
                          valid_from=datetime(2026, 10, 28),
                          valid_to=datetime(2026, 11, 14),
                          note="校准过期,读数存疑"),
        SensorCalibration(device_id=ob3.id, status="VALID",
                          valid_from=datetime(2026, 11, 14)),
    ])
    # ---- 准时登记的维修/故障记录(T01 故障在趋势运行后晚到登记) ----
    db.add_all([
        MaintenanceEvent(train_id=t02.id, component_install_id=i_t02a.id,
                         type="REPAIR",
                         occurred_at=datetime(2026, 10, 10, 9),
                         recorded_at=datetime(2026, 10, 10, 10),
                         description="牵引电机 TM-T02-A 寿命到期,更换为 TM-T02-B"),
        MaintenanceEvent(train_id=t03.id, component_install_id=i_t03a.id,
                         type="FAULT",
                         occurred_at=datetime(2026, 10, 22, 10, 15),
                         recorded_at=datetime(2026, 10, 22, 11, 0),
                         description="牵引电机接地故障,列车救援回库"),
        MaintenanceEvent(train_id=t03.id, component_install_id=i_t03a.id,
                         type="REPAIR",
                         occurred_at=datetime(2026, 10, 23, 14),
                         recorded_at=datetime(2026, 10, 23, 15),
                         description="更换牵引电机 TM-T03-B"),
    ])

    # ---- 历史运行 ----
    def D(m, d, h=8):
        return datetime(2026, m, d, h)

    # T01: 8 月高温(季节负荷) + 9-10 月健康基线 + 11 月慢性劣化
    t01_runs = [
        ("RUN-2001", D(8, 3), 33, 1.28, False), ("RUN-2002", D(8, 10), 32, 1.28, False),
        ("RUN-2003", D(8, 17), 34, 1.28, False), ("RUN-2004", D(8, 24), 33, 1.28, False),
        ("RUN-2005", D(8, 27), 33, 1.28, False),  # 高温高能耗但落在高温层基线内
        ("RUN-2006", D(9, 5), 20, 1.00, False), ("RUN-2007", D(9, 12), 21, 1.00, False),
        ("RUN-2008", D(9, 19), 19, 1.00, False), ("RUN-2009", D(9, 26), 20, 1.00, False),
        ("RUN-2010", D(10, 3), 18, 1.00, False), ("RUN-2011", D(10, 10), 19, 1.00, False),
        ("RUN-2012", D(10, 17), 18, 1.00, False),
        ("RUN-2013", D(10, 20, 12), 18, 1.04, True),   # 临时限速,条件调整后不超限
        ("RUN-2014", D(10, 24), 19, 1.00, False),
        ("RUN-2015", D(10, 27), 18, 1.04, True),
        ("RUN-2016", D(11, 1), 17, 1.04, False),   # 劣化起步,尚在基线内
        ("RUN-2017", D(11, 4), 17, 1.12, False),   # 以下持续超限 -> 慢性劣化
        ("RUN-2018", D(11, 7), 16, 1.20, False),
        ("RUN-2019", D(11, 10), 16, 1.28, False),
        ("RUN-2020", D(11, 13), 15, 1.36, False),
        ("RUN-2021", D(11, 16), 15, 1.45, False),
    ]
    for key, start, temp, factor, restr in t01_runs:
        gen_health_run(db, st, key, t01, l1, s1, sub1, c01, ob1, start,
                       temp, 800, factor, restriction=restr)

    # T02: 9 月健康(旧电机) + 10-10 更换 + 新周期(校准过期/失效/条件缺失)
    t02_runs = [
        ("RUN-2022", D(9, 2, 9), 19, 1.00, c02a, True),
        ("RUN-2023", D(9, 9, 9), 20, 1.00, c02a, True),
        ("RUN-2024", D(9, 16, 9), 20, 1.00, c02a, True),
        ("RUN-2025", D(9, 23, 9), 19, 1.00, c02a, True),
        ("RUN-2026", D(9, 30, 9), 18, 1.00, c02a, True),
        ("RUN-2027", D(10, 7, 9), 18, 1.00, c02a, True),
        ("RUN-2028", D(10, 12, 9), 17, 1.00, c02b, True),   # 校准过期 -> 降级
        ("RUN-2029", D(10, 19, 9), 18, 1.00, c02b, True),   # 校准过期 -> 降级
        ("RUN-2030", D(10, 26, 9), 18, 1.00, c02b, True),
        ("RUN-2031", D(11, 2, 9), 16, 1.30, c02b, True),    # 校准失效:高能耗取消资格
        ("RUN-2032", D(11, 9, 9), 15, 1.00, c02b, False),   # 条件缺失 -> 取消资格
        ("RUN-2033", D(11, 16, 9), 15, 1.00, c02b, True),
    ]
    for key, start, temp, factor, consist, with_cond in t02_runs:
        gen_health_run(db, st, key, t02, l1, s1, sub1, consist, ob2, start,
                       temp, 800, factor, with_condition=with_cond)

    # T03: 健康基线 + 10-22 突发故障 + 更换后新周期(校准过期下持续偏高)
    t03_runs = [
        ("RUN-2034", D(9, 3, 10), 21, 1.00), ("RUN-2035", D(9, 10, 10), 20, 1.00),
        ("RUN-2036", D(9, 17, 10), 19, 1.00), ("RUN-2037", D(9, 24, 10), 18, 1.00),
        ("RUN-2038", D(10, 1, 10), 18, 1.00), ("RUN-2039", D(10, 8, 10), 17, 1.00),
        ("RUN-2040", D(10, 15, 10), 17, 1.00),
        ("RUN-2041", D(10, 22, 10), 16, 1.55),  # 突发剧增 -> 突发报警
        ("RUN-2042", D(10, 30, 10), 14, 1.18),  # 校准过期:持续超限但报警降级
        ("RUN-2043", D(11, 6, 10), 12, 1.18),
        ("RUN-2044", D(11, 13, 10), 11, 1.18),
    ]
    for key, start, temp, factor in t03_runs:
        gen_health_run(db, st, key, t03, l2, s3, sub3, c03, ob3, start,
                       temp, 500, factor)
    db.flush()

    # ---- 趋势运行 第一批:截至 11-20(T01 故障尚未登记) ----
    run_trend_batch(db, datetime(2026, 11, 20), "SEED")

    # ---- 维修记录晚到:T01 故障 11-15 发生、11-25 才登记 ----
    # 触发新一轮趋势运行:故障前 10 天窗口重标为 EXCLUDED_PRE_FAULT 并移出基线,
    # 旧报警保留当时依据,维修回查可见该故障确认了既有报警。
    record_maintenance(db, t01.id, "FAULT", datetime(2026, 11, 15, 20),
                       "牵引电机轴承故障确认(维修中心补录,晚到 10 天)",
                       component_install_id=i_t01.id,
                       recorded_at=datetime(2026, 11, 25, 9))


def seed():
    drop_all()
    create_all()
    db: Session = SessionLocal()
    try:
        # ---- 用户 ----
        db.add_all([
            User(username="admin", password="admin123", role="admin",
                 token="tok-admin"),
            User(username="viewer", password="viewer123", role="viewer",
                 token="tok-viewer"),
        ])
        # ---- 线路/区段 ----
        l1 = Line(code="L1", name="1号线")
        l2 = Line(code="L2", name="2号线")
        db.add_all([l1, l2]); db.flush()
        # ---- 列车/编组/设备 ----
        t01 = Train(code="T01", vehicle_model="M1")
        t02 = Train(code="T02", vehicle_model="M1")
        t03 = Train(code="T03", vehicle_model="M2")
        db.add_all([t01, t02, t03]); db.flush()
        c01 = Consist(train_id=t01.id, code="6B", valid_from=datetime(2026, 1, 1))
        c02a = Consist(train_id=t02.id, code="6B", valid_from=datetime(2026, 1, 1),
                       valid_to=datetime(2026, 10, 20, 10, 15))
        c02b = Consist(train_id=t02.id, code="4B", valid_from=datetime(2026, 10, 20, 10, 15))
        c03 = Consist(train_id=t03.id, code="6A", valid_from=datetime(2026, 1, 1))
        db.add_all([c01, c02a, c02b, c03]); db.flush()
        ob1 = Device(serial="OB-T01", kind="ONBOARD", train_id=t01.id)
        ob2 = Device(serial="OB-T02", kind="ONBOARD", train_id=t02.id)
        ob3 = Device(serial="OB-T03", kind="ONBOARD", train_id=t03.id)
        sub1 = Device(serial="SUB-S1", kind="SUBSTATION", station_name="A站牵引所")
        sub2 = Device(serial="SUB-S2", kind="SUBSTATION", station_name="B站牵引所")
        sub3 = Device(serial="SUB-S3", kind="SUBSTATION", station_name="D站牵引所")
        db.add_all([ob1, ob2, ob3, sub1, sub2, sub3]); db.flush()
        s1 = Section(line_id=l1.id, code="S1", name="A站-B站", seq=1, length_km=10,
                     multiplier_zone="Z1", substation_device_id=sub1.id)
        s2 = Section(line_id=l1.id, code="S2", name="B站-C站", seq=2, length_km=12,
                     multiplier_zone="Z1", substation_device_id=sub2.id)
        s3 = Section(line_id=l2.id, code="S3", name="D站-E站", seq=1, length_km=8,
                     multiplier_zone="Z2", substation_device_id=sub3.id)
        db.add_all([s1, s2, s3]); db.flush()
        # ---- 配置版本 ----
        db.add_all([
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 1, 1),
                          payload={"OB-T01": 1.0, "OB-T02": 1.0, "OB-T03": 1.0},
                          note="车载表初始倍率"),
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 10, 20, 10, 40),
                          payload={"OB-T02": 2.0},
                          note="T02 进入倍率区间 Z2,倍率 1.0->2.0"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 1, 1),
                          payload={"default": True, "stations": {}},
                          note="默认接受回馈上网"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 11, 1),
                          payload={"default": True, "stations": {"SUB-S3": False}},
                          note="D站牵引所回馈装置检修,不接受回馈上网"),
            ConfigVersion(kind="LOSS_RATE", valid_from=datetime(2026, 1, 1),
                          payload={"rate": 0.06}, note="线网损耗经验值 6%"),
        ])

        st = {d.serial: Counter() for d in [ob1, ob2, ob3, sub1, sub2, sub3]}

        # ============ RUN-1001 跨区段折返 ============
        r1 = mk_run(db, "RUN-1001", t01, l1, D1, D1 + timedelta(minutes=85),
                    44, 1200, "早高峰-1")
        db.add(RunConsist(run_id=r1.id, consist_id=c01.id, valid_from=D1))
        ev(db, r1, D1, "ENTER_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=30), "STOP", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=35), "START_MOVE", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=45), "TURNBACK", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=65), "EXIT_SECTION", s2, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=65), "ENTER_SECTION", s1, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=85), "EXIT_SECTION", s1, "DOWN")
        gen_onboard(db, ob1, st["OB-T01"], D1, D1 + timedelta(minutes=85))
        gen_supply(db, sub1, st["SUB-S1"], D1, D1 + timedelta(minutes=90),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D1 + timedelta(minutes=20),
                   D1 + timedelta(minutes=70), imp_rate=0.60, exp_rate=0.10)
        process(db, r1)

        # ============ RUN-1002 设备重启+倍率切换+换编组 ============
        t0 = D1 + timedelta(hours=2)  # 10:00-10:50,避开 RUN-1001 的供电窗口
        r2 = mk_run(db, "RUN-1002", t02, l1, t0, t0 + timedelta(minutes=50),
                    22, 800, "早高峰-1", mode="CM")
        db.add_all([
            RunConsist(run_id=r2.id, consist_id=c02a.id, valid_from=t0,
                       valid_to=t0 + timedelta(minutes=15)),
            RunConsist(run_id=r2.id, consist_id=c02b.id,
                       valid_from=t0 + timedelta(minutes=15)),
        ])
        ev(db, r2, t0, "ENTER_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=15), "CONSIST_CHANGE", s1, "UP",
           payload={"consist_id": c02b.id})
        ev(db, r2, t0 + timedelta(minutes=25), "EXIT_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=25), "ENTER_SECTION", s2, "UP")
        ev(db, r2, t0 + timedelta(minutes=50), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob2, st["OB-T02"], t0, t0 + timedelta(minutes=50),
                    reset_at=t0 + timedelta(minutes=10),
                    mult_change_at=t0 + timedelta(minutes=40), mult_after=2.0)
        gen_supply(db, sub1, st["SUB-S1"], t0, t0 + timedelta(minutes=30),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], t0 + timedelta(minutes=25),
                   t0 + timedelta(minutes=55), imp_rate=0.60, exp_rate=0.10)
        process(db, r2)

        # ============ RUN-1003 回馈受限 ============
        r3 = mk_run(db, "RUN-1003", t03, l2, D3, D3 + timedelta(minutes=25),
                    8, 500, "早高峰-2")
        db.add(RunConsist(run_id=r3.id, consist_id=c03.id, valid_from=D3))
        ev(db, r3, D3, "ENTER_SECTION", s3, "UP")
        ev(db, r3, D3 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D3, D3 + timedelta(minutes=25),
                    rates=(0.5, 0.4, 0.05))  # 大回馈
        gen_supply(db, sub3, st["SUB-S3"], D3, D3 + timedelta(minutes=30),
                   imp_rate=0.30, exp_rate=0.0)  # 电网不接受回馈
        process(db, r3)

        # ============ RUN-1004 车载与供电表差异 ============
        r4 = mk_run(db, "RUN-1004", t01, l1, D4, D4 + timedelta(minutes=40),
                    22, 600, "平峰-1")
        db.add(RunConsist(run_id=r4.id, consist_id=c01.id, valid_from=D4))
        ev(db, r4, D4, "ENTER_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r4, D4 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob1, st["OB-T01"], D4, D4 + timedelta(minutes=40))
        gen_supply(db, sub1, st["SUB-S1"], D4, D4 + timedelta(minutes=25),
                   imp_rate=0.78, exp_rate=0.05)  # 差异偏大
        gen_supply(db, sub2, st["SUB-S2"], D4 + timedelta(minutes=20),
                   D4 + timedelta(minutes=45), imp_rate=0.74, exp_rate=0.05)
        process(db, r4)

        # ============ RUN-1005 补传读数 ============
        r5 = mk_run(db, "RUN-1005", t02, l1, D5, D5 + timedelta(minutes=40),
                    22, 900, "平峰-2")
        db.add(RunConsist(run_id=r5.id, consist_id=c02b.id, valid_from=D5))
        ev(db, r5, D5, "ENTER_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r5, D5 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        # v1: 16:15 之后车载缺测(通信中断),先按缺测发布
        gen_onboard(db, ob2, st["OB-T02"], D5, D5 + timedelta(minutes=40),
                    skip_after=D5 + timedelta(minutes=15))
        gen_supply(db, sub1, st["SUB-S1"], D5, D5 + timedelta(minutes=25),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D5 + timedelta(minutes=20),
                   D5 + timedelta(minutes=45), imp_rate=0.60, exp_rate=0.10)
        process(db, r5)  # 发布 v1(低覆盖)
        # 迟到补传:16:20-16:40 的读数在 1 小时后到达 -> 触发 v2
        late_rows = []
        t = D5 + timedelta(minutes=20)
        while t <= D5 + timedelta(minutes=40):
            st["OB-T02"].seq += 1
            late_rows.append(MeterReading(
                device_id=ob2.id, sampled_at=t,
                received_at=t + timedelta(hours=1),  # 迟到
                seq=st["OB-T02"].seq,
                traction_cum=round(st["OB-T02"].v["traction"], 3),
                regen_cum=round(st["OB-T02"].v["regen"], 3),
                aux_cum=round(st["OB-T02"].v["aux"], 3),
                multiplier=1.0, comm_status="OK"))
            st["OB-T02"].v["traction"] += 0.5 * 5
            st["OB-T02"].v["regen"] += 0.1 * 5
            st["OB-T02"].v["aux"] += 0.05 * 5
            t += STEP
        db.add_all(late_rows); db.flush()
        r5v2 = reprocess_run(db, r5, note="迟到补传")
        process(db, r5v2, note="迟到补传触发新版本")

        # ============ RUN-1006 供电表缺失 ============
        r6 = mk_run(db, "RUN-1006", t03, l2, D6, D6 + timedelta(minutes=25),
                    8, 450, "早高峰-2")
        db.add(RunConsist(run_id=r6.id, consist_id=c03.id, valid_from=D6))
        ev(db, r6, D6, "ENTER_SECTION", s3, "UP")
        ev(db, r6, D6 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D6, D6 + timedelta(minutes=25))
        # SUB-S3 在该窗口无任何读数(表计检修)-> 供电侧未知
        process(db, r6)

        # ---- 既有运行的条件变量(健康分析输入;RUN-1005 取最新版本 v2) ----
        db.add_all([
            RunCondition(run_id=r1.id, ambient_temp_c=18, weather="CLEAR"),
            RunCondition(run_id=r2.id, ambient_temp_c=18, weather="CLEAR"),
            RunCondition(run_id=r3.id, ambient_temp_c=12, weather="CLEAR"),
            RunCondition(run_id=r4.id, ambient_temp_c=19, weather="CLEAR"),
            RunCondition(run_id=r5v2.id, ambient_temp_c=18, weather="CLEAR"),
            RunCondition(run_id=r6.id, ambient_temp_c=11, weather="CLEAR"),
        ])

        # ============ 设备健康:档案 + 六类验收场景 ============
        seed_health(db, st, t01, t02, t03, l1, l2, s1, s3, sub1, sub3,
                    c01, c02a, c02b, c03, ob1, ob2, ob3)

        db.commit()
        print("seed ok: 6 个能耗场景 + 44 个健康场景运行,含两批趋势运行")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
