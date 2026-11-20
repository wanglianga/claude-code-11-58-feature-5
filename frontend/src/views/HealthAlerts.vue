<template>
  <div>
    <el-card>
      <template #header>
        健康报警:条件调整后持续偏离才报警;旧报警保留当时依据,晚到维修记录不改写
      </template>
      <el-table :data="alerts" size="small">
        <el-table-column prop="alert_key" label="报警" width="90" />
        <el-table-column prop="train" label="列车" width="70" />
        <el-table-column prop="component_serial" label="部件" width="110" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="ALERT_TYPE_TAG[row.type]" size="small">
              {{ ALERT_TYPE_LABEL[row.type] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="资格" width="90">
          <template #default="{ row }">
            <el-tag :type="row.eligibility === 'QUALIFIED' ? 'success' : 'warning'" size="small">
              {{ row.eligibility === 'QUALIFIED' ? '具备' : '降级' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="超限窗口" width="200">
          <template #default="{ row }">
            {{ row.first_excess_at.slice(0, 10) }} ~ {{ row.last_excess_at.slice(0, 10) }}
          </template>
        </el-table-column>
        <el-table-column prop="persist_runs" label="持续(趟)" width="80" />
        <el-table-column label="最大残差" width="90">
          <template #default="{ row }">{{ (row.max_residual_pct * 100).toFixed(1) }}%</template>
        </el-table-column>
        <el-table-column prop="excess_kwh" label="超额能耗(kWh)" width="120" />
        <el-table-column label="维修回查" min-width="140">
          <template #default="{ row }">
            <el-tag v-if="row.confirmed_by.length" type="danger" size="small">
              已被故障/维修记录确认
            </el-tag>
            <span v-else class="dim">待回查</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="open(row.id)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="show" size="62%" :title="detail ? `${detail.alert_key} 报警详情与维修回查` : ''">
      <template v-if="detail">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="列车/部件">
            {{ detail.train }} / {{ detail.component_serial }}
          </el-descriptions-item>
          <el-descriptions-item label="类型">{{ ALERT_TYPE_LABEL[detail.type] }}</el-descriptions-item>
          <el-descriptions-item label="资格">
            {{ detail.eligibility === 'QUALIFIED' ? '具备' : '降级(校准过期)' }}
          </el-descriptions-item>
          <el-descriptions-item label="持续长度">{{ detail.persist_runs }} 趟</el-descriptions-item>
          <el-descriptions-item label="最大残差">
            {{ (detail.max_residual_pct * 100).toFixed(1) }}%
          </el-descriptions-item>
          <el-descriptions-item label="超额能耗">{{ detail.excess_kwh }} kWh</el-descriptions-item>
        </el-descriptions>

        <el-card shadow="never" style="margin-top: 14px">
          <template #header>当时依据(快照,晚到记录不改写)</template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="分析截止">{{ detail.basis.as_of.slice(0, 16) }}</el-descriptions-item>
            <el-descriptions-item label="触发">{{ TRIGGER_LABEL[detail.basis.trigger] }}</el-descriptions-item>
            <el-descriptions-item label="健康期标注" :span="2">
              {{ detail.basis.health_note }}
            </el-descriptions-item>
            <el-descriptions-item label="产生时已知维修记录" :span="2">
              <span v-if="!detail.basis.known_events.length" class="dim">无</span>
              <el-tag v-for="e in detail.basis.known_events" :key="e.id" size="small"
                      style="margin-right: 6px">
                {{ MAINT_TYPE_LABEL[e.type] }} {{ e.occurred_at.slice(0, 10) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="分析参数" :span="2">
              <code>持续≥{{ detail.basis.params.persist_runs }}趟 · 突发>{{
                detail.basis.params.sudden_pct * 100 }}% · 故障前排除{{
                detail.basis.params.pre_fault_exclude_days }}天 · 最少{{
                detail.basis.params.min_baseline_samples }}样本</code>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never" style="margin-top: 14px">
          <template #header>支撑残差点(原始能耗 × 条件变量)</template>
          <el-table :data="detail.points" size="small">
            <el-table-column prop="run_key" label="运行" width="100" />
            <el-table-column label="时刻" width="105">
              <template #default="{ row }">{{ row.started_at.slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column label="实际/基线" width="170">
              <template #default="{ row }">
                <b>{{ fmtVal(row.actual_per_km, 3) }}</b>
                <span class="dim"> / {{ row.baseline_mid }} (上限 {{ row.baseline_hi }})</span>
              </template>
            </el-table-column>
            <el-table-column label="残差" width="80">
              <template #default="{ row }">
                <span class="bad">{{ (row.residual_pct * 100).toFixed(1) }}%</span>
              </template>
            </el-table-column>
            <el-table-column label="条件" width="150">
              <template #default="{ row }">
                <span v-if="row.condition" class="dim">
                  {{ row.condition.ambient_temp_c }}°C · {{ row.condition.passengers }}人
                  <template v-if="row.condition.speed_restriction">· 限速</template>
                </span>
              </template>
            </el-table-column>
            <el-table-column label="原始能耗" min-width="150">
              <template #default="{ row }">
                <span class="dim">净 {{ row.energy.net_kwh }} kWh ·
                  {{ row.energy.distance_km }} km · {{ row.energy.quality }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" style="margin-top: 14px">
          <template #header>维修回查(同一部件安装周期的维修/故障记录)</template>
          <el-table :data="detail.linked_maintenance" size="small">
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag :type="MAINT_TYPE_TAG[row.type]" size="small">
                  {{ MAINT_TYPE_LABEL[row.type] }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="发生" width="105">
              <template #default="{ row }">{{ row.occurred_at.slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column label="登记" width="105">
              <template #default="{ row }">{{ row.recorded_at.slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column label="晚到" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.late" type="danger" size="small">晚到</el-tag>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column label="确认报警" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.confirms" type="danger" size="small">确认</el-tag>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" min-width="200" />
          </el-table>
          <p v-if="!detail.linked_maintenance.length" class="dim">
            该部件安装周期内暂无维修/故障记录。
          </p>
        </el-card>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'
import { ALERT_TYPE_LABEL, ALERT_TYPE_TAG, MAINT_TYPE_LABEL, MAINT_TYPE_TAG, TRIGGER_LABEL, fmtVal } from '../utils'

const alerts = ref([])
const show = ref(false)
const detail = ref(null)

async function open(id) {
  detail.value = (await api.get(`/health/alerts/${id}`)).data
  show.value = true
}

onMounted(async () => {
  alerts.value = (await api.get('/health/alerts')).data
})
</script>

<style scoped>
.bad { color: #d45555; font-weight: 600; }
.dim { color: #999; font-size: 12px; }
code { font-size: 12px; }
</style>
