<template>
  <div v-if="run">
    <el-card>
      <template #header>
        <div class="bar">
          <span>
            {{ run.run_key }}
            <el-tag size="small" :type="run.version > 1 ? 'warning' : 'info'">v{{ run.version }}</el-tag>
            <el-tag size="small" type="success" style="margin-left:4px">{{ run.status }}</el-tag>
          </span>
          <span class="meta">
            {{ run.train }}({{ run.vehicle_model }}) · {{ run.line }} · {{ run.driver_mode }} ·
            {{ run.distance_km }}km · {{ run.passengers }}人 · {{ run.shift }}
          </span>
        </div>
      </template>
      <el-alert v-if="run.supersedes_id" type="warning" :closable="false"
                title="本版本由迟到补传触发,旧版本与旧指标保留原水位" />
      <el-table :data="run.segments" size="small" style="margin-top: 10px">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="frag-wrap">
              <div v-for="f in row.fragments" :key="f.id" class="frag">
                <div>
                  <el-tag size="small" :type="f.source === 'ONBOARD' ? 'primary' : 'success'">
                    {{ SOURCE_LABEL[f.source] }}
                  </el-tag>
                  <el-tag size="small" :type="QUALITY_TAG[f.quality]" style="margin-left:6px">
                    {{ f.quality }}
                  </el-tag>
                  <span class="cov">覆盖率 {{ (f.coverage * 100).toFixed(0) }}%</span>
                </div>
                <span v-for="(band, k) in f.quantities" :key="k" class="band">
                  {{ Q_LABEL[k] }}: <b>{{ fmtBand(band) }}</b>
                </span>
                <div v-if="f.detail && f.detail.resets && f.detail.resets.length" class="reset">
                  重启记录:<span v-for="(r, i) in f.detail.resets" :key="i">
                    {{ r.at }} 由 {{ r.dropped_from }} 归零(空窗{{ r.gap_minutes }}min)
                  </span>
                </div>
                <div v-if="f.detail && f.detail.note" class="reset">{{ f.detail.note }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="seq" label="#" width="50" />
        <el-table-column prop="section" label="区段" width="80" />
        <el-table-column prop="direction" label="方向" width="70" />
        <el-table-column prop="start_ts" label="开始" width="170" />
        <el-table-column prop="end_ts" label="结束" width="170" />
        <el-table-column label="切断原因" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.cut_reason === 'DEVICE_RESET' ? 'danger' : 'info'">
              {{ CUT_LABEL[row.cut_reason] || row.cut_reason }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="车载净能耗(kWh)">
          <template #default="{ row }">{{ netOf(row, 'ONBOARD') }}</template>
        </el-table-column>
        <el-table-column label="供电净受电(kWh)">
          <template #default="{ row }">{{ netOf(row, 'SUPPLY') }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card style="margin-top: 16px">
      <template #header>
        <div class="bar">
          <span>账实核对</span>
          <el-radio-group v-model="method" size="small" @change="loadRecon">
            <el-radio-button value="RUN_ALIGNED">运行对齐(路径A)</el-radio-button>
            <el-radio-button value="GRID_ALIGNED">网格对齐(路径B)</el-radio-button>
          </el-radio-group>
        </div>
      </template>
      <template v-if="recon">
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="可比较性">
            <el-tag :type="COMPARABLE_TAG[recon.comparable]">{{ recon.comparable }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="核对范围">
            {{ recon.range_start.slice(11, 16) }} ~ {{ recon.range_end.slice(11, 16) }}
          </el-descriptions-item>
          <el-descriptions-item label="车载覆盖率">
            {{ (recon.coverage_onboard * 100).toFixed(0) }}%
          </el-descriptions-item>
          <el-descriptions-item label="供电覆盖率">
            {{ (recon.coverage_supply * 100).toFixed(0) }}%
          </el-descriptions-item>
          <el-descriptions-item label="车载净能耗">{{ fmtBand(recon.onboard_net) }}</el-descriptions-item>
          <el-descriptions-item label="供电净受电">{{ fmtBand(recon.supply_net) }}</el-descriptions-item>
          <el-descriptions-item label="差异(供电-车载)">{{ fmtBand(recon.diff) }}</el-descriptions-item>
          <el-descriptions-item label="方法">{{ recon.method }}</el-descriptions-item>
        </el-descriptions>
        <el-row :gutter="16" style="margin-top: 12px">
          <el-col :span="12">
            <h4>差异去向归因</h4>
            <el-table :data="attrRows" size="small">
              <el-table-column prop="k" label="去向" width="140" />
              <el-table-column prop="v" label="kWh" />
            </el-table>
          </el-col>
          <el-col :span="12">
            <h4>不可判定原因</h4>
            <el-table :data="recon.undetermined" size="small">
              <el-table-column prop="code" label="代码" width="220" />
              <el-table-column prop="detail" label="说明" />
            </el-table>
          </el-col>
        </el-row>
      </template>
      <el-empty v-else description="该方法暂无核对结论" />
    </el-card>

    <el-card v-if="method === 'GRID_ALIGNED' && grid.length" style="margin-top: 16px">
      <template #header>网格累计(区段 × 15min 时间桶,跨格按时间比例分摊)</template>
      <el-table :data="grid" size="small">
        <el-table-column prop="section" label="区段" width="80" />
        <el-table-column prop="bucket_start" label="桶起点" width="170" />
        <el-table-column label="车载牵引"><template #default="{ row }">{{ fmtBand(row.onboard.traction) }}</template></el-table-column>
        <el-table-column label="车载回馈"><template #default="{ row }">{{ fmtBand(row.onboard.regen) }}</template></el-table-column>
        <el-table-column label="供电受电"><template #default="{ row }">{{ fmtBand(row.supply.import) }}</template></el-table-column>
        <el-table-column label="供电返送"><template #default="{ row }">{{ fmtBand(row.supply.export) }}</template></el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { COMPARABLE_TAG, CUT_LABEL, Q_LABEL, QUALITY_TAG, SOURCE_LABEL, fmtBand } from '../utils'

const route = useRoute()
const run = ref(null)
const recon = ref(null)
const grid = ref([])
const method = ref('RUN_ALIGNED')

const ATTR_LABEL = { line_loss: '线网损耗', regen_not_returned: '回馈未上网',
                     metering_delay: '计量延迟', unknown: '不可知残差' }
const attrRows = computed(() => {
  if (!recon.value) return []
  const a = recon.value.attribution
  return Object.keys(ATTR_LABEL).map((k) => ({ k: ATTR_LABEL[k], v: a[k] }))
})

function netOf(seg, source) {
  const f = seg.fragments.find((x) => x.source === source)
  if (!f) return '-'
  const q = f.quantities
  if (source === 'ONBOARD') {
    if (q.traction.v === null) return '未知'
    return (q.traction.v + q.aux.v - q.regen.v).toFixed(2)
  }
  if (!q.import || q.import.v === null) return '未知'
  return (q.import.v - q.export.v).toFixed(2)
}

async function loadRecon() {
  recon.value = null
  try {
    recon.value = (await api.get(`/runs/${route.params.id}/reconciliation`,
                                 { params: { method: method.value } })).data
  } catch { /* 无结论 */ }
  if (method.value === 'GRID_ALIGNED') {
    grid.value = (await api.get(`/runs/${route.params.id}/grid`)).data
  }
}

onMounted(async () => {
  run.value = (await api.get(`/runs/${route.params.id}`)).data
  await loadRecon()
})
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.meta { color: #888; font-size: 13px; }
.frag-wrap { padding: 4px 16px; }
.frag { margin-bottom: 10px; }
.band { margin-right: 18px; font-size: 13px; }
.cov { margin-left: 8px; color: #888; font-size: 12px; }
.reset { color: #c45656; font-size: 12px; margin-top: 4px; }
h4 { margin: 0 0 8px; color: #555; }
</style>
