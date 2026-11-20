# 轨道交通牵引能耗运行事实与对标平台

判断一趟列车的**牵引取电、制动回馈、辅助负荷**与**站场供电表**是否描述了同一段可计量运行,
形成可用于线路对标、节能评估和事故复盘的能耗事实;并在能耗事实之上,面向设备健康提供
**条件化残差趋势**:在相同车型、线路、载客、天气和运行约束下生成预期能耗区间,识别
条件调整后仍持续偏离的能耗信号,而非把高能耗本身当故障证据。

技术栈:Vue 3 + Element Plus(前端)/ FastAPI + SQLAlchemy(后端)/ PostgreSQL(存储)。

## 核心规则

| 环节 | 规则 |
| --- | --- |
| 采集接入 | 保存原始累计值、采样时刻、设备序列号、通信状态、计量倍率;迟到采样按到达时间识别 |
| 运行切分 | 按列车、编组、线路、区段、停车与折返记录切成可计量片段;**换编组、进入不同倍率区间、设备重启/读数倒退必须切断** |
| 能量求值 | 累计值重启或倒退**不直接相减**,只累加单调段,空窗按上限速率扩展不确定区间上界;供电表缺失或覆盖不足时输出**未知范围**,不以车载值冒充闭合计量 |
| 账实核对 | 计算列车侧与供电侧可比较范围、差异去向(线损/回馈未上网/计量延迟/不可知)与不可判定原因 |
| 双路径 | 路径A 运行对齐:逐片段对齐两侧时间边界再汇总;路径B 网格对齐:区段×15min 固定网格累计双侧能量、运行映射进网格,跨格按时间比例分摊为区间。两者对同一运行范围独立给出可比较能耗与差异边界 |
| 指标服务 | 按运行/区段/班次/车型发布单位里程、单位载客能耗与回馈比例,带来源覆盖率 |
| 迟到补传 | 只生成新运行版本;**已发布指标保留原水位**,新指标带差异说明,复盘可回查完整链路 |
| 健康档案 | 保存部件安装/更换区间、检修与故障时间(发生/登记双时刻)、传感器校准状态、允许关注的设备层级(未启用层级不做分析) |
| 条件化基线 | 在 车型×线路×温度带×载客带×运行约束 的**已确认健康运行**分层上取中位数±K·MAD 生成预期能耗区间;样本不足逐级放宽条件回退并记录所用层级;全层不足输出"无基线" |
| 健康期标注 | 已知故障前 10 天窗口标记 EXCLUDED_PRE_FAULT:**只评分、不进基线**,防止故障前隐性劣化污染训练集合;故障发生后(未修复)标记 POST_FAULT,不再参与报警 |
| 趋势运行 | 按**部件安装周期**执行,部件更换后重新起算;汇总每个周期的超额能耗残差、持续长度与覆盖不足区间 |
| 报警资格 | 校准失效/过期、运行条件缺失、能耗未知或为估计值 → **降低或取消报警资格,不默认为正常**;连续超限≥3 趟报慢性劣化,孤立剧增>35% 报突发 |
| 维修晚到 | 维修记录晚到触发新一轮趋势运行并重标健康期;**旧报警保留当时依据**,新分析结论一致时不重复报警;报警可回查关联的维修/故障记录 |

## 一键启动(Docker Compose)

```bash
docker compose up --build
```

- 前端:http://localhost:8080
- 后端 API:http://localhost:8000(Swagger 文档 `/docs`,FastAPI 自带)
- 首次启动自动建库并注入五类能耗场景 + 六类健康场景演示数据;`SEED_ON_START=0` 可关闭重建

## 本地开发

前置:PostgreSQL 运行于 127.0.0.1:5432(库/用户/密码均为 `ccdev`)。

```bash
# 后端(端口 8000)
cd backend
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv/bin/python -m app.seed          # 建表 + 注入验收场景数据
.venv/bin/uvicorn app.main:app --port 8000

# 前端(端口 5173,代理 /api -> 8000)
cd frontend
npm install
npm run dev                           # 打开 http://localhost:5173
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理(可采集写入) |
| viewer | viewer123 | 只读 |

## 验证方式

### 自动化测试(已实际执行,28 项全部通过)

```bash
cd backend
.venv/bin/python -m pytest tests/ -q    # 使用独立测试库 ccdev_test
```

覆盖:计数器重启/倒退不相减、倍率切断、未知范围不冒充 0、五类能耗场景的端到端结论、
迟到补传触发新版本且旧指标保留、双路径同范围结果一致、复盘链路完整;
以及健康侧:季节负荷条件化后不报警、慢性劣化持续超限报警、突发故障单点报警、
部件更换后趋势重新起算、校准失效/条件缺失取消报警资格(不默认正常)、
维修记录迟到重标健康期且旧报警保留当时依据、维修回查关联确认。

### 五类能耗验收场景(种子数据自带,可在页面直接核对)

| 运行 | 场景 | 页面验证点 |
| --- | --- | --- |
| RUN-1001 | 跨区段折返 | 运行详情:折返/停车切断、方向 UP→DOWN 翻转;核对 FULL,差异=线损+不可知 |
| RUN-1002 | 设备重启+换编组+倍率切换 | 片段切断原因含 DEVICE_RESET/CONSIST_CHANGE/MULTIPLIER_CHANGE;重启段质量 ESTIMATED、上界>下界 |
| RUN-1003 | 回馈受限 | 核对归因"回馈未上网">0,假设中 regen_accepted=false |
| RUN-1004 | 车载与供电表差异 | 差异 10.4 kWh 分解为线损/回馈未上网/不可知,归因之和=差异 |
| RUN-1005 | 补传读数 | v1 指标为"未知"(覆盖 37.5%),v2 闭合;指标页 diff_note 含"迟到补传";复盘页见历史水位 |
| RUN-1006 | 供电表缺失 | 核对 NONE,供电净受电"未知",不可判定原因 SUPPLY_COVERAGE_INSUFFICIENT |

页面路径:总览 → 运行与能量(点运行进详情,可切换 路径A/路径B)→ 指标服务 → 复盘回查。

### 六类健康验收场景(种子数据自带,可在页面直接核对)

| 场景 | 数据 | 页面验证点 |
| --- | --- | --- |
| 季节负荷变化 | RUN-2001~2005(33°C 高温) | 残差趋势:绝对能耗 ≈1.05 kWh/km 高于常态基线,但落在高温条件层(L1)基线内,**不报警** |
| 部件更换 | T02 电机 10-10 更换(TM-T02-A→B) | 健康档案:两个安装周期;残差趋势:新周期从 10-12 重新起算,基线来自车队同条件健康样本 |
| 慢性劣化 | RUN-2017~2021(T01,11 月) | 健康报警 ALT-0001:连续 5 趟超限,最大残差 45%,超额 11.56 kWh;详情含当时依据快照 |
| 突发故障 | RUN-2041(T03,10-22) | 健康报警 ALT-0002:单趟残差 +56% 报突发;维修回查被当日故障记录确认 |
| 校准失效 | OB-T02 过期/失效、OB-T03 过期 | 残差趋势(T02-B):RUN-2031 校准失效期间高能耗**取消资格不报警也不判正常**;T03-B 校准过期下持续超限 → ALT-0003 **资格降级**报警 |
| 维修记录迟到 | T01 故障 11-15 发生、11-25 登记 | 残差趋势 TR6(触发=维修晚到重分析):RUN-2018~2020 重标"故障前窗口(排除出基线)",RUN-2021 标 POST_FAULT;ALT-0001 依据快照仍为"无已知故障",维修回查显示该晚到故障确认了报警 |

页面路径:总览 → 健康档案(层级/部件/校准/维修记录,可登记晚到记录)→ 残差趋势(残差图+资格原因+覆盖不足区间)→ 健康报警(依据快照+能耗条件关联+维修回查)。

### 手工 API 验证示例

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 双路径核对结论对比(同一运行范围)
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=RUN_ALIGNED"  -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=GRID_ALIGNED" -H "Authorization: Bearer $TOKEN"

# 健康档案 / 趋势运行 / 报警
curl -s "http://localhost:8000/api/health/profile"     -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/health/trend-runs"  -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/health/trend-runs/6" -H "Authorization: Bearer $TOKEN"  # 晚到重分析后的健康期
curl -s "http://localhost:8000/api/health/alerts/1"    -H "Authorization: Bearer $TOKEN"   # 依据快照+维修回查

# 登记一条晚到维修记录 -> 自动触发新一轮趋势运行(健康期重标)
curl -s -X POST http://localhost:8000/api/health/maintenance -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"train_id":2,"type":"OVERHAUL","occurred_at":"2026-11-03T10:00:00","description":"晚到登记示例"}'

# 补登运行条件(此前 RUN-2032 因条件缺失取消资格),再手动触发趋势运行
curl -s -X POST http://localhost:8000/api/health/conditions -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"run_key":"RUN-2032","ambient_temp_c":15,"weather":"CLEAR"}'
curl -s -X POST http://localhost:8000/api/health/trend-runs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"component_install_id":3}'

# 补传一条迟到读数 -> 自动生成新运行版本并重发指标
curl -s -X POST http://localhost:8000/api/readings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[{"device_serial":"OB-T01","sampled_at":"2026-10-21T14:05:00",
        "received_at":"2026-10-21T18:00:00","seq":99001,
        "traction_cum":0,"regen_cum":0,"aux_cum":0,"multiplier":1.0}]'
```

## 目录结构

```
backend/app/
  models.py              # 领域模型:读数/运行/片段/能量/网格/核对/指标/配置版本
                         #   + 健康域:部件安装/维修记录/校准/关注层级/运行条件/趋势运行/残差点/报警
  services/energy.py     # 计数器求差:重启倒退、倍率切断、不确定区间
  services/segmentation.py  # 运行切分(停车/折返/换编组/倍率/重启)
  services/evaluate.py   # 片段求值(路径A) + 网格重建(路径B)
  services/reconcile.py  # 账实核对:可比范围、差异归因、不可判定
  services/metrics.py    # 指标发布与迟到版本化
  services/health.py     # 条件化残差趋势:健康期标注、分层鲁棒基线、趋势运行、报警资格
  api/routes.py          # REST API(登录鉴权)
  api/health.py          # 健康 API:档案/趋势/报警/维修登记/条件补登
  seed.py                # 五类能耗场景 + 六类健康场景演示数据
backend/tests/           # pytest:单元 + 场景端到端(能耗 18 项 + 健康 10 项)
frontend/src/views/      # 总览/采集/运行/指标/复盘 + 健康档案/残差趋势/健康报警
docker-compose.yml       # 一键启动(db + backend + frontend)
```

## 已执行的验证结论

- `pytest`:28 项全部通过(测试库 ccdev_test,与开发库隔离)。
- 本地开发模式全链路冒烟:后端 uvicorn + 前端 vite dev 代理下,登录、总览、
  运行详情双路径核对、指标、复盘接口均返回预期数据;健康档案、残差趋势(含
  晚到重分析后的健康期重标)、健康报警(依据快照+维修回查)、晚到维修登记触发
  重分析、条件补登+手动趋势运行等接口均实测返回预期数据。
- 前端 `npm run build` 构建通过。
- 本交付容器内无 docker 命令,Docker 镜像未实际构建;Compose 配置按标准
  多阶段构建编写,本地开发验证均使用原生运行时完成。
