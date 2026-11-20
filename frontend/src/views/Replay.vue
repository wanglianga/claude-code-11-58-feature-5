<template>
  <div>
    <el-card>
      <template #header>复盘回查:指标由哪些采样片段、配置版本和核对结论构成</template>
      <el-select v-model="metricId" filterable placeholder="选择指标" style="width: 520px"
                 @change="load">
        <el-option v-for="m in metrics" :key="m.id"
                   :label="`#${m.id} ${SCOPE_LABEL[m.scope]}/${m.scope_key} ${METRIC_LABEL[m.name]} = ${fmtVal(m.value, 4)} (v${m.run_version})`"
                   :value="m.id" />
      </el-select>
    </el-card>

    <template v-if="data">
      <el-card style="margin-top: 16px">
        <template #header>指标水位与历史</template>
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="范围">{{ SCOPE_LABEL[data.metric.scope] }}/{{ data.metric.scope_key }}</el-descriptions-item>
          <el-descriptions-item label="指标">{{ METRIC_LABEL[data.metric.name] }}</el-descriptions-item>
          <el-descriptions-item label="当前水位">
            <b>{{ fmtVal(data.metric.value, 4) }}</b>(覆盖 {{ (data.metric.coverage * 100).toFixed(0) }}%)
          </el-descriptions-item>
          <el-descriptions-item label="方法/版本">{{ data.metric.method }} · v{{ data.metric.run_version }}</el-descriptions-item>
          <el-descriptions-item label="差异说明" :span="4">
            <span class="note">{{ data.metric.diff_note || '首次发布' }}</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-timeline v-if="data.history.length" style="margin-top: 14px">
          <el-timeline-item v-for="h in data.history" :key="h.id"
                            :timestamp="h.published_at" type="warning">
            历史水位 #{{ h.id }}:{{ fmtVal(h.value, 4) }}(运行 v{{ h.run_version }})
            <span v-if="h.diff_note" class="note"> — {{ h.diff_note }}</span>
          </el-timeline-item>
        </el-timeline>
      </el-card>

      <el-card v-for="r in data.runs" :key="r.id" style="margin-top: 16px">
        <template #header>
          来源运行 {{ r.run_key }} v{{ r.version }}({{ r.train }} · {{ r.line }} ·
          {{ r.started_at.slice(0, 16) }} ~ {{ r.ended_at.slice(11, 16) }})
        </template>
        <el-table :data="segRows(r)" size="small">
          <el-table-column prop="seq" label="#" width="50" />
          <el-table-column prop="section" label="区段" width="80" />
          <el-table-column prop="direction" label="方向" width="70" />
          <el-table-column label="时间窗" width="180">
            <template #default="{ row }">{{ row.start_ts.slice(11) }} ~ {{ row.end_ts.slice(11) }}</template>
          </el-table-column>
          <el-table-column label="切断" width="110">
            <template #default="{ row }">{{ CUT_LABEL[row.cut_reason] || row.cut_reason }}</template>
          </el-table-column>
          <el-table-column label="车载 牵引/回馈/辅助">
            <template #default="{ row }">{{ row.onb }}</template>
          </el-table-column>
          <el-table-column label="供电 受电/返送">
            <template #default="{ row }">{{ row.sup }}</template>
          </el-table-column>
          <el-table-column label="质量" width="110">
            <template #default="{ row }">
              <el-tag v-for="q in row.qualities" :key="q" size="small"
                      :type="QUALITY_TAG[q]" style="margin-right:4px">{{ q }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>核对结论</template>
        <el-table :data="data.reconciliations" size="small">
          <el-table-column prop="method" label="方法" width="130" />
          <el-table-column label="可比较" width="90">
            <template #default="{ row }">
              <el-tag :type="COMPARABLE_TAG[row.comparable]" size="small">{{ row.comparable }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="车载净能耗"><template #default="{ row }">{{ fmtBand(row.onboard_net) }}</template></el-table-column>
          <el-table-column label="供电净受电"><template #default="{ row }">{{ fmtBand(row.supply_net) }}</template></el-table-column>
          <el-table-column label="差异"><template #default="{ row }">{{ fmtBand(row.diff) }}</template></el-table-column>
          <el-table-column label="归因(线损/回馈未上网/延迟/不可知)" min-width="220">
            <template #default="{ row }">
              {{ row.attribution.line_loss }} / {{ row.attribution.regen_not_returned }} /
              {{ row.attribution.metering_delay }} / {{ row.attribution.unknown }}
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card style="margin-top: 16px">
        <template #header>计量配置版本(复盘时点有效)</template>
        <el-table :data="data.config_versions" size="small">
          <el-table-column prop="kind" label="类型" width="140" />
          <el-table-column label="内容" min-width="300">
            <template #default="{ row }"><code>{{ row.payload }}</code></template>
          </el-table-column>
          <el-table-column prop="valid_from" label="生效自" width="170" />
          <el-table-column prop="note" label="说明" min-width="200" />
        </el-table>
        <p class="foot">关联原始读数 {{ data.reading_ids.length }} 条(采集数据页可按设备回查)。</p>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { COMPARABLE_TAG, CUT_LABEL, METRIC_LABEL, QUALITY_TAG, SCOPE_LABEL, fmtBand, fmtVal } from '../utils'

const route = useRoute()
const metrics = ref([])
const metricId = ref(null)
const data = ref(null)

function segRows(run) {
  return run.segments.map((s) => {
    const onb = s.fragments.find((f) => f.source === 'ONBOARD')
    const sup = s.fragments.find((f) => f.source === 'SUPPLY')
    return {
      ...s,
      onb: onb ? `${fmtBand(onb.quantities.traction)} / ${fmtBand(onb.quantities.regen)} / ${fmtBand(onb.quantities.aux)}` : '-',
      sup: sup ? `${fmtBand(sup.quantities.import)} / ${fmtBand(sup.quantities.export)}` : '-',
      qualities: [...new Set(s.fragments.map((f) => f.quality))],
    }
  })
}

async function load() {
  if (!metricId.value) return
  data.value = (await api.get(`/metrics/${metricId.value}/replay`)).data
}

onMounted(async () => {
  metrics.value = (await api.get('/metrics')).data
  if (route.query.metric) {
    metricId.value = Number(route.query.metric)
    await load()
  }
})
</script>

<style scoped>
.note { color: #b88230; font-size: 12px; }
.foot { color: #999; font-size: 12px; }
code { font-size: 12px; }
</style>
