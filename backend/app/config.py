import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 能量求值与核对的全局常量(可随配置版本演进,见 ConfigVersion)
MAX_TRACTION_RATE_KWH_PER_MIN = 40.0   # 重启/失连空窗的牵引上限速率,用于不确定区间上界
MAX_AUX_RATE_KWH_PER_MIN = 3.0
LINE_LOSS_RATE = 0.06                  # 线网传输损耗经验比例(相对供电侧受电)
COVERAGE_THRESHOLD = 0.95              # 可闭合核对的最低时间覆盖率
GRID_BUCKET_MINUTES = 15               # 网格对齐路径的固定时间桶
LATE_ARRIVAL_SECONDS = 300             # 采样-到达超过该值视为迟到采样

# 设备健康:条件化残差趋势分析参数(可被 HealthWatchConfig.params 覆盖)
HEALTH_MIN_BASELINE_SAMPLES = 4        # 分层基线每层最少健康样本数,不足则向粗层回退
HEALTH_PERSIST_RUNS = 3                # 连续超限运行数达到该值才报慢性劣化
HEALTH_SUDDEN_PCT = 0.35               # 单点残差超过基线中值该比例 -> 突发报警
HEALTH_PRE_FAULT_EXCLUDE_DAYS = 10     # 已知故障前该天数内的运行排除出基线训练(防隐性劣化污染)
HEALTH_BAND_FLOOR_PCT = 0.06           # 基线区间半宽下限(相对中位数),防零宽区间
HEALTH_MAD_K = 3.0                     # 区间半宽 = K * MAD * 1.4826(鲁棒标准差)
LATE_MAINTENANCE_SECONDS = 3600        # 维修记录 登记-发生 超过该值视为晚到,触发健康期重标
