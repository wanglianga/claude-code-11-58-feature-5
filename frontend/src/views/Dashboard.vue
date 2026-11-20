<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="6" v-for="c in cards" :key="c.label">
        <el-card shadow="hover">
          <div class="num">{{ c.value }}</div>
          <div class="label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card>
          <template #header>账实核对闭合情况</template>
          <el-progress type="dashboard" :percentage="fullPct" :width="160">
            <span>{{ fullPct }}%</span>
          </el-progress>
          <div class="label" style="margin-top: 8px">
            完全闭合 {{ ov.comparable_full }} / 部分闭合 {{ ov.comparable_partial }} /
            共 {{ ov.reconciliations }} 条核对结论
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>验收场景速览</template>
          <el-table :data="scenarios" size="small">
            <el-table-column prop="run" label="运行" width="110" />
            <el-table-column prop="scene" label="场景" />
            <el-table-column label="核对" width="90">
              <template #default="{ row }">
                <el-tag :type="COMPARABLE_TAG[row.comparable]" size="small">
                  {{ row.comparable }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card>
          <template #header>
            <div class="bar">
              <span>设备健康报警(条件化残差趋势)</span>
              <el-button link type="primary" @click="$router.push('/health/alerts')">
                全部报警
              </el-button>
            </div>
          </template>
          <el-table :data="alerts" size="small">
            <el-table-column prop="alert_key" label="报警" width="90" />
            <el-table-column prop="train" label="列车" width="70" />
            <el-table-column label="类型" width="100">
              <template #default="{ row }">
                <el-tag :type="ALERT_TYPE_TAG[row.type]" size="small">
                  {{ ALERT_TYPE_LABEL[row.type] }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="资格" width="80">
              <template #default="{ row }">
                {{ row.eligibility === 'QUALIFIED' ? '具备' : '降级' }}
              </template>
            </el-table-column>
            <el-table-column label="持续/残差" width="120">
              <template #default="{ row }">
                {{ row.persist_runs }}趟 · {{ (row.max_residual_pct * 100).toFixed(0) }}%
              </template>
            </el-table-column>
            <el-table-column label="维修回查" min-width="120">
              <template #default="{ row }">
                <el-tag v-if="row.confirmed_by.length" type="danger" size="small">已确认</el-tag>
                <span v-else class="dim">待回查</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>健康验收场景速览</template>
          <el-table :data="healthScenarios" size="small">
            <el-table-column prop="scene" label="场景" width="150" />
            <el-table-column prop="expect" label="预期结论" min-width="240" />
            <el-table-column label="查看" width="80">
              <template #default="{ row }">
                <el-button link type="primary" @click="$router.push(row.to)">前往</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import { ALERT_TYPE_LABEL, ALERT_TYPE_TAG, COMPARABLE_TAG } from '../utils'

const ov = ref({})
const runs = ref([])
const alerts = ref([])
const healthScenarios = [
  { scene: '季节负荷变化', expect: '夏季高温运行绝对能耗高,但落在高温条件层基线内,不报警', to: '/health/trends' },
  { scene: '部件更换', expect: 'T02/T03 更换牵引电机后,新安装周期趋势重新起算', to: '/health' },
  { scene: '慢性劣化', expect: 'T01 残差连续 5 趟超限 -> 慢性劣化报警(具备资格)', to: '/health/alerts' },
  { scene: '突发故障', expect: 'T03 单趟残差 +56% -> 突发报警,当日故障记录确认', to: '/health/alerts' },
  { scene: '校准失效', expect: '校准过期降级/失效取消资格,高能耗不报警也不默认正常', to: '/health/trends' },
  { scene: '维修记录迟到', expect: 'T01 故障晚到 10 天 -> 健康期重标,旧报警保留当时依据', to: '/health/trends' },
]
const scenarios = computed(() => {
  const map = {
    'RUN-1001': '跨区段折返', 'RUN-1002': '设备重启/换编组/倍率切换',
    'RUN-1003': '回馈受限', 'RUN-1004': '车载与供电表差异',
    'RUN-1005': '补传读数(迟到)', 'RUN-1006': '供电表缺失',
  }
  return runs.value
    .filter((r) => map[r.run_key])
    .map((r) => ({ run: `${r.run_key} v${r.version}`, scene: map[r.run_key],
                    comparable: r._comparable || '-' }))
})
const cards = computed(() => [
  { label: '线路 / 区段', value: `${ov.value.lines ?? '-'} / ${ov.value.sections ?? '-'}` },
  { label: '运行(版本)', value: `${ov.value.runs ?? '-'} (${ov.value.run_versions ?? '-'})` },
  { label: '原始读数', value: ov.value.readings ?? '-' },
  { label: '已发布指标', value: ov.value.metrics ?? '-' },
])
const fullPct = computed(() =>
  ov.value.reconciliations
    ? Math.round((ov.value.comparable_full / ov.value.reconciliations) * 100) : 0)

onMounted(async () => {
  ov.value = (await api.get('/meta/overview')).data
  alerts.value = (await api.get('/health/alerts')).data
  const rs = (await api.get('/runs?all_versions=true')).data
  for (const r of rs) {
    try {
      const rec = (await api.get(`/runs/${r.id}/reconciliation?method=RUN_ALIGNED`)).data
      r._comparable = rec.comparable
    } catch { r._comparable = '-' }
  }
  runs.value = rs
})
</script>

<style scoped>
.num { font-size: 26px; font-weight: 600; color: #1c5d8c; }
.label { color: #888; font-size: 13px; margin-top: 4px; }
.bar { display: flex; justify-content: space-between; align-items: center; }
.dim { color: #999; font-size: 12px; }
</style>
