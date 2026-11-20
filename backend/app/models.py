"""领域模型:采集、运行切分、能量求值、账实核对、指标与复盘。

约定:
- 所有时间戳为 naive UTC(datetime),由写入方保证。
- 能量单位统一为 kWh;累计表读数为表码原值,倍率单独记录,求值时相乘。
- 带不确定范围的能量用 {"v": 估值, "lo": 下界, "hi": 上界} 的 JSON 表示;
  lo/hi 为 None 表示该侧未知(不闭合),禁止用估值冒充闭合计量。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base, engine


# ---------------- 基础档案 ----------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)  # 演示环境明文,生产应哈希
    role = Column(String(32), nullable=False, default="viewer")  # admin|viewer
    token = Column(String(64), unique=True, nullable=False)


class Line(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(64), nullable=False)


class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False)           # 区内顺序
    length_km = Column(Float, nullable=False)
    multiplier_zone = Column(String(32), nullable=False, default="Z1")  # 计量倍率区间
    substation_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    line = relationship("Line")


class Train(Base):
    __tablename__ = "trains"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    vehicle_model = Column(String(32), nullable=False)  # 车型


class Consist(Base):
    """编组版本:同一列车换编组后必须切断能量片段。"""
    __tablename__ = "consists"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    code = Column(String(32), nullable=False)       # 如 6B / 6A
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    train = relationship("Train")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True)
    serial = Column(String(64), unique=True, nullable=False)  # 设备序列号
    kind = Column(String(16), nullable=False)       # ONBOARD|SUBSTATION
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=True)
    station_name = Column(String(64), nullable=True)  # 变电所名
    train = relationship("Train")


# ---------------- 采集接入 ----------------

class MeterReading(Base):
    """原始累计值采样。车载表填 traction/regen/aux;变电所表填 import/export。"""
    __tablename__ = "meter_readings"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    sampled_at = Column(DateTime, nullable=False, index=True)   # 采样时刻(表侧)
    received_at = Column(DateTime, nullable=False)              # 平台到达时刻
    seq = Column(Integer, nullable=False)                       # 设备采样序列号
    traction_cum = Column(Float, nullable=True)
    regen_cum = Column(Float, nullable=True)
    aux_cum = Column(Float, nullable=True)
    import_cum = Column(Float, nullable=True)   # 变电所受电累计
    export_cum = Column(Float, nullable=True)   # 变电所返送(回馈上网)累计
    multiplier = Column(Float, nullable=False, default=1.0)     # 计量倍率
    comm_status = Column(String(16), nullable=False, default="OK")  # OK|DEGRADED|LOST
    device = relationship("Device")
    __table_args__ = (UniqueConstraint("device_id", "seq", name="uq_reading_device_seq"),)


# ---------------- 运行切分 ----------------

class Run(Base):
    """一趟可计量运行。迟到补传会产生 version+1 的新行,旧版本保留供复盘。"""
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    run_key = Column(String(64), nullable=False, index=True)  # 业务键,跨版本稳定
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    driver_mode = Column(String(16), nullable=False, default="AM")  # AM|CM 司机模式
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    distance_km = Column(Float, nullable=False)
    passengers = Column(Integer, nullable=False, default=0)
    shift = Column(String(32), nullable=False, default="")          # 班次
    status = Column(String(16), nullable=False, default="OPEN")     # OPEN|EVALUATED|PUBLISHED
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    train = relationship("Train")
    line = relationship("Line")


class RunEvent(Base):
    """切分输入:停车/起动/折返/进出区段/换编组等原始记录。"""
    __tablename__ = "run_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False)
    type = Column(String(24), nullable=False)
    # ENTER_SECTION|EXIT_SECTION|STOP|START_MOVE|TURNBACK|CONSIST_CHANGE
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    direction = Column(String(8), nullable=True)  # UP|DOWN
    payload = Column(JSON, nullable=True)         # 如 {"consist_id": 2}
    run = relationship("Run")


class RunConsist(Base):
    """运行内的编组有效期(换编组切断)。"""
    __tablename__ = "run_consists"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    consist = relationship("Consist")


class RunSegment(Base):
    """可计量运行片段:同车、同编组、同倍率区间,由停车/折返/换编组/换倍率/设备重启切断。"""
    __tablename__ = "run_segments"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    direction = Column(String(8), nullable=False)
    start_ts = Column(DateTime, nullable=False)
    end_ts = Column(DateTime, nullable=False)
    seq = Column(Integer, nullable=False)
    cut_reason = Column(String(24), nullable=False)  # START|STOP|TURNBACK|SECTION_EXIT|CONSIST_CHANGE|MULTIPLIER_CHANGE|DEVICE_RESET|END
    run = relationship("Run")
    section = relationship("Section")


# ---------------- 能量求值 ----------------

class EnergyFragment(Base):
    """片段级能量事实。quantities 示例:
    车载: {"traction": {"v":..,"lo":..,"hi":..}, "regen": {...}, "aux": {...}}
    供电: {"import": {...}, "export": {...}}
    """
    __tablename__ = "energy_fragments"
    id = Column(Integer, primary_key=True)
    run_segment_id = Column(Integer, ForeignKey("run_segments.id"), nullable=False, index=True)
    source = Column(String(8), nullable=False)      # ONBOARD|SUPPLY
    quantities = Column(JSON, nullable=False)
    quality = Column(String(16), nullable=False)    # GOOD|ESTIMATED|UNKNOWN
    coverage = Column(Float, nullable=False)        # 有效计量时间覆盖率 0..1
    detail = Column(JSON, nullable=True)            # resets/gaps/notes 等解释
    segment = relationship("RunSegment")


class GridCell(Base):
    """网格对齐路径:固定 区段×时间桶 双侧能量累计。"""
    __tablename__ = "grid_cells"
    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False, index=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    bucket_minutes = Column(Integer, nullable=False)
    onboard = Column(JSON, nullable=False)  # {"traction": {...}, "regen": {...}, "aux": {...}}
    supply = Column(JSON, nullable=False)   # {"import": {...}, "export": {...}}
    run_ids = Column(JSON, nullable=False)  # 落入该桶的运行 id 列表
    section = relationship("Section")


# ---------------- 账实核对 ----------------

class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    run_version = Column(Integer, nullable=False)
    method = Column(String(16), nullable=False)     # RUN_ALIGNED|GRID_ALIGNED
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    comparable = Column(String(8), nullable=False)  # FULL|PARTIAL|NONE
    coverage_onboard = Column(Float, nullable=False)
    coverage_supply = Column(Float, nullable=False)
    onboard_net = Column(JSON, nullable=False)      # {"v","lo","hi"} 车载净能耗(牵引+辅助-回馈)
    supply_net = Column(JSON, nullable=False)       # {"v","lo","hi"} 供电净受电(受电-返送)
    diff = Column(JSON, nullable=False)             # supply_net - onboard_net 的区间
    attribution = Column(JSON, nullable=False)      # {line_loss, regen_not_returned, metering_delay, unknown}
    undetermined = Column(JSON, nullable=False)     # 不可判定原因列表
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


# ---------------- 指标与复盘 ----------------

class Metric(Base):
    """已发布指标。迟到数据只产生新版本指标,旧指标保留原水位与差异说明。"""
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)      # RUN|SECTION|SHIFT|VEHICLE_MODEL
    scope_key = Column(String(64), nullable=False)  # 如 run_key / section code / 班次 / 车型
    name = Column(String(32), nullable=False)       # KWH_PER_KM|KWH_PER_PASSENGER|REGEN_RATIO|NET_KWH
    value = Column(Float, nullable=True)            # None 表示不可判定
    coverage = Column(Float, nullable=False)
    method = Column(String(16), nullable=False)
    run_version = Column(Integer, nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    supersedes_id = Column(Integer, ForeignKey("metrics.id"), nullable=True)
    diff_note = Column(Text, nullable=True)         # 与上一已发布水位的差异说明
    source_run_ids = Column(JSON, nullable=False)   # 构成该指标的运行版本 id


class ConfigVersion(Base):
    """计量配置版本:倍率、设备映射、回馈上网是否被电网接受等,供复盘追溯。"""
    __tablename__ = "config_versions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(24), nullable=False)       # MULTIPLIER|DEVICE_MAP|REGEN_ACCEPT|LOSS_RATE
    payload = Column(JSON, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)


# ---------------- 设备健康:档案、条件化残差趋势与报警 ----------------

class ComponentInstall(Base):
    """部件安装/更换区间。同一列车同一部件类型更换后产生新安装周期,
    残差趋势按安装周期重新起算,不跨周期累计持续长度。"""
    __tablename__ = "component_installs"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False, index=True)
    component_type = Column(String(32), nullable=False)  # TRACTION_MOTOR|INVERTER|...
    serial = Column(String(64), nullable=False)          # 部件序列号
    installed_at = Column(DateTime, nullable=False)
    removed_at = Column(DateTime, nullable=True)         # None = 在役
    note = Column(String(255), nullable=True)
    train = relationship("Train")


class MaintenanceEvent(Base):
    """检修/故障/维修记录。occurred_at 为发生时刻,recorded_at 为登记时刻;
    晚到记录(recorded_at 明显晚于 occurred_at)会触发健康期重标与新趋势运行,
    但不改写已产生报警的当时依据。"""
    __tablename__ = "maintenance_events"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False, index=True)
    component_install_id = Column(Integer, ForeignKey("component_installs.id"),
                                  nullable=True)  # None = 整车级记录
    type = Column(String(16), nullable=False)     # FAULT|REPAIR|OVERHAUL
    occurred_at = Column(DateTime, nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    description = Column(String(255), nullable=False, default="")
    train = relationship("Train")
    component_install = relationship("ComponentInstall")


class SensorCalibration(Base):
    """传感器(车载表)校准状态区间。失效/过期期间运行的报警资格被降低或取消,
    这些运行不默认为正常,只标记为不可判定。"""
    __tablename__ = "sensor_calibrations"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    status = Column(String(16), nullable=False)   # VALID|EXPIRED|FAILED
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)
    device = relationship("Device")


class HealthWatchConfig(Base):
    """允许关注的设备层级闸门:只有启用的 (层级,部件类型) 才会生成趋势运行。"""
    __tablename__ = "health_watch_configs"
    id = Column(Integer, primary_key=True)
    level = Column(String(16), nullable=False)    # COMPONENT|TRAIN
    component_type = Column(String(32), nullable=True)  # level=COMPONENT 时必填
    enabled = Column(Integer, nullable=False, default=1)  # 1|0
    params = Column(JSON, nullable=False, default=dict)   # 覆盖默认分析参数
    note = Column(String(255), nullable=True)


class RunCondition(Base):
    """运行条件变量:天气/载客/运行约束。缺失时该运行不具备报警资格
    (无法做条件化调整,不默认为正常)。"""
    __tablename__ = "run_conditions"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, unique=True)
    ambient_temp_c = Column(Float, nullable=False)
    weather = Column(String(16), nullable=False, default="CLEAR")  # CLEAR|RAIN|SNOW
    speed_restriction = Column(Integer, nullable=False, default=0)  # 0|1 临时限速等约束
    source = Column(String(32), nullable=False, default="SEED")
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


class TrendRun(Base):
    """趋势运行:对某部件安装周期的一次完整残差分析(可重入,每次生成新行)。
    summary 汇总超额能耗残差、持续长度与覆盖不足区间。"""
    __tablename__ = "trend_runs"
    id = Column(Integer, primary_key=True)
    component_install_id = Column(Integer, ForeignKey("component_installs.id"),
                                  nullable=False, index=True)
    as_of = Column(DateTime, nullable=False)      # 分析截止时刻(只纳入此前事实)
    trigger = Column(String(24), nullable=False)  # SEED|MANUAL|LATE_MAINTENANCE
    params = Column(JSON, nullable=False)         # 本次分析参数快照
    summary = Column(JSON, nullable=False)        # n_points/excess_kwh/max_persist/coverage_gaps 等
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    component_install = relationship("ComponentInstall")


class ResidualPoint(Base):
    """趋势运行内每个运行的残差点:实际单位能耗 vs 条件化基线区间。"""
    __tablename__ = "residual_points"
    id = Column(Integer, primary_key=True)
    trend_run_id = Column(Integer, ForeignKey("trend_runs.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    started_at = Column(DateTime, nullable=False)
    actual_per_km = Column(Float, nullable=True)  # None = 能耗未知
    baseline_lo = Column(Float, nullable=True)
    baseline_mid = Column(Float, nullable=True)
    baseline_hi = Column(Float, nullable=True)
    baseline_level = Column(Integer, nullable=True)    # 1 最细 .. 5 最粗;None=无基线
    baseline_samples = Column(Integer, nullable=False, default=0)
    residual_pct = Column(Float, nullable=True)   # (actual-mid)/mid
    excess = Column(Integer, nullable=False, default=0)  # 1 = 超出基线上界
    eligible = Column(String(16), nullable=False)  # ELIGIBLE|DOWNGRADED|INELIGIBLE
    eligibility_reasons = Column(JSON, nullable=False, default=list)
    health_state = Column(String(24), nullable=False)  # HEALTHY|EXCLUDED_PRE_FAULT|POST_FAULT
    condition_snapshot = Column(JSON, nullable=True)   # 条件变量快照(缺失为 None)
    energy_snapshot = Column(JSON, nullable=True)      # 原始能耗快照
    trend_run = relationship("TrendRun")
    run = relationship("Run")


class HealthAlert(Base):
    """健康报警:由一次趋势运行产生,basis 保存产生时的全部依据快照;
    晚到维修记录触发的新分析会产生新报警,旧报警不被改写。"""
    __tablename__ = "health_alerts"
    id = Column(Integer, primary_key=True)
    alert_key = Column(String(32), unique=True, nullable=False)
    trend_run_id = Column(Integer, ForeignKey("trend_runs.id"), nullable=False)
    component_install_id = Column(Integer, ForeignKey("component_installs.id"),
                                  nullable=False, index=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    type = Column(String(16), nullable=False)     # DEGRADATION|SUDDEN
    eligibility = Column(String(16), nullable=False)  # QUALIFIED|DOWNGRADED
    first_excess_at = Column(DateTime, nullable=False)
    last_excess_at = Column(DateTime, nullable=False)
    persist_runs = Column(Integer, nullable=False)     # 持续长度(连续超限运行数)
    max_residual_pct = Column(Float, nullable=False)
    excess_kwh = Column(Float, nullable=False)         # 累计超额能耗残差
    point_ids = Column(JSON, nullable=False)           # 支撑本报警的残差点
    basis = Column(JSON, nullable=False)               # 当时依据快照
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    trend_run = relationship("TrendRun")
    component_install = relationship("ComponentInstall")
    train = relationship("Train")


def create_all():
    from . import models as _  # noqa
    Base.metadata.create_all(engine)


def drop_all():
    Base.metadata.drop_all(engine)
