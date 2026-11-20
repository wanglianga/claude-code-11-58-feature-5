<template>
  <div>
    <el-card>
      <template #header>
        残差趋势:同一条件(车型×线路×温度×载客×运行约束)下的预期能耗区间 vs 实际单位能耗
      </template>
      <el-select v-model="trId" filterable placeholder="选择趋势运行" style="width: 640px"
                 @change="load">
        <el-option v-for="t in trendRuns" :key="t.id"
                   :label="`TR${t.id} · ${t.train}/${t.component_serial} · ${TRIGGER_LABEL[t.trigger]} · 截至 ${t.as_of.slice(0, 10)} · 超限${t.summary.n_excess}/${t.summary.n_points}`"
                   :value="t.id" />
      </el-select>
    </el-card>

    <template v-if="data">
      <el-card style="margin-top: 16px">
        <template #header>
          趋势汇总({{ data.train }}/{{ data.component_serial }} · 安装周期
          {{ data.install_period.from.slice(0, 10) }} ~ {{ data.install_period.to ? data.install_period.to.slice(0, 10) : '在役' }})
        </template>
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="残差点">{{ s.n_points }}</el-descriptions-item>
          <el-descriptions-item label="具备/降级/取消资格">
            {{ s.n_eligible }} / {{ s.n_downgraded }} / {{ s.n_ineligible }}
          </el-descriptions-item>
          <el-descriptions-item label="超额能耗残差">{{ s.excess_kwh }} kWh</el-descriptions-item>
          <el-descriptions-item label="最长持续超限">{{ s.max_persist }} 趟</el-descriptions-item>
          <el-descriptions-item label="资格取消的超限" :span="2">
            {{ s.suppressed_excess }} 趟(不报警也不默认正常)
          </el-descriptions-item>
          <el-descriptions-item label="健康期标注" :span="2">{{ s.health_note }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="s.coverage_gaps && s.coverage_gaps.length" class="gaps">
          覆盖不足区间:
          <el-tag v-for="(g, i) in s.coverage_gaps" :key="i" size="small" type="info"
                  style="margin-right: 6px">
            {{ g.from.slice(0, 10) }}<template v-if="g.from !== g.to"> ~ {{ g.to.slice(0, 10) }}</template>
            ({{ g.reasons.map((r) => REASON_LABEL[r] || r).join('/') }})
          </el-tag>
        </div>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>
          残差图(灰柱=基线区间;点=实际单位能耗,红=超限,灰=资格取消/降级)
        </template>
        <svg :viewBox="`0 0 ${W} ${H}`" class="chart">
          <line :x1="PAD" :y1="H - PAD" :x2="W - 10" :y2="H - PAD" stroke="#ccc" />
          <text v-for="t in yTicks" :key="t" :x="4" :y="y(t) + 4" class="axis">{{ t }}</text>
          <line v-for="t in yTicks" :key="'g' + t" :x1="PAD" :y1="y(t)" :x2="W - 10"
                :y2="y(t)" stroke="#f0f0f0" />
          <g v-for="(p, i) in data.points" :key="p.id">
            <rect v-if="p.baseline_lo !== null" :x="x(i) - 7" :y="y(p.baseline_hi)"
                  width="14" :height="Math.max(2, y(p.baseline_lo) - y(p.baseline_hi))"
                  fill="#dbe9f5" stroke="#a8c6e0" stroke-width="0.5" />
            <circle v-if="p.actual_per_km !== null" :cx="x(i)" :cy="y(p.actual_per_km)"
                    r="4.5" :fill="dotColor(p)">
              <title>{{ p.run_key }} {{ p.started_at.slice(0, 10) }}
实际 {{ p.actual_per_km }} kWh/km · 基线 [{{ p.baseline_lo }} ~ {{ p.baseline_hi }}]
{{ ELIGIBLE_LABEL[p.eligible] }} {{ p.eligibility_reasons.map((r) => REASON_LABEL[r] || r).join('/') }}</title>
            </circle>
            <text v-if="i % 2 === 0" :x="x(i)" :y="H - PAD + 14" class="axis"
                  text-anchor="middle">{{ p.started_at.slice(5, 10) }}</text>
          </g>
        </svg>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>残差点明细(资格取消不默认为正常)</template>
        <el-table :data="data.points" size="small" max-height="420">
          <el-table-column prop="run_key" label="运行" width="100" />
          <el-table-column label="时刻" width="105">
            <template #default="{ row }">{{ row.started_at.slice(0, 16) }}</template>
          </el-table-column>
          <el-table-column label="实际(kWh/km)" width="110">
            <template #default="{ row }"><b>{{ fmtVal(row.actual_per_km, 3) }}</b></template>
          </el-table-column>
          <el-table-column label="基线区间" width="190">
            <template #default="{ row }">
              <span v-if="row.baseline_mid !== null">
                {{ row.baseline_lo }} ~ {{ row.baseline_hi }}
                <span class="dim">(L{{ row.baseline_level }}/{{ row.baseline_samples }}样本)</span>
              </span>
              <span v-else class="dim">无基线</span>
            </template>
          </el-table-column>
          <el-table-column label="残差" width="90">
            <template #default="{ row }">
              <span :class="{ bad: row.excess }">
                {{ row.residual_pct === null ? '-' : (row.residual_pct * 100).toFixed(1) + '%' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="条件" width="150">
            <template #default="{ row }">
              <span v-if="row.condition" class="dim">
                {{ row.condition.ambient_temp_c }}°C/{{ row.condition.temp_band }}
                · {{ row.condition.passengers }}人/{{ row.condition.load_band }}
                <template v-if="row.condition.speed_restriction">· 限速</template>
              </span>
              <span v-else class="dim">条件缺失</span>
            </template>
          </el-table-column>
          <el-table-column label="健康标签" width="170">
            <template #default="{ row }">
              {{ HEALTH_STATE_LABEL[row.health_state] }}
            </template>
          </el-table-column>
          <el-table-column label="报警资格" min-width="180">
            <template #default="{ row }">
              <el-tag :type="ELIGIBLE_TAG[row.eligible]" size="small">
                {{ ELIGIBLE_LABEL[row.eligible] }}
              </el-tag>
              <span v-for="r in row.eligibility_reasons" :key="r" class="reason">
                {{ REASON_LABEL[r] || r }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { ELIGIBLE_LABEL, ELIGIBLE_TAG, HEALTH_STATE_LABEL, REASON_LABEL, TRIGGER_LABEL, fmtVal } from '../utils'

const route = useRoute()
const trendRuns = ref([])
const trId = ref(null)
const data = ref(null)
const W = 960, H = 280, PAD = 40

const s = computed(() => data.value?.summary || {})
const yMax = computed(() => {
  if (!data.value) return 1
  let m = 0.1
  for (const p of data.value.points) {
    if (p.baseline_hi !== null) m = Math.max(m, p.baseline_hi)
    if (p.actual_per_km !== null) m = Math.max(m, p.actual_per_km)
  }
  return m * 1.08
})
const yTicks = computed(() => {
  const step = yMax.value / 4
  return [0, 1, 2, 3, 4].map((i) => Number((step * i).toFixed(2)))
})
const x = (i) => PAD + (i + 0.5) * ((W - PAD - 20) / (data.value?.points.length || 1))
const y = (v) => H - PAD - (v / yMax.value) * (H - PAD - 20)

function dotColor(p) {
  if (p.eligible === 'INELIGIBLE') return '#a8abb2'
  if (p.eligible === 'DOWNGRADED') return '#e6a23c'
  return p.excess ? '#d45555' : '#4d9e4d'
}

async function load() {
  if (!trId.value) return
  data.value = (await api.get(`/health/trend-runs/${trId.value}`)).data
}

onMounted(async () => {
  trendRuns.value = (await api.get('/health/trend-runs')).data
  if (route.query.tr) {
    trId.value = Number(route.query.tr)
  } else if (trendRuns.value.length) {
    trId.value = trendRuns.value[0].id
  }
  await load()
})
</script>

<style scoped>
.chart { width: 100%; background: #fff; }
.axis { font-size: 10px; fill: #999; }
.bad { color: #d45555; font-weight: 600; }
.dim { color: #999; font-size: 12px; }
.reason { color: #b88230; font-size: 12px; margin-left: 6px; }
.gaps { margin-top: 10px; font-size: 13px; color: #666; }
</style>
