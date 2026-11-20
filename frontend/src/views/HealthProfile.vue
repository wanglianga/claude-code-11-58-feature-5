<template>
  <div>
    <el-row :gutter="16">
      <el-col :span="10">
        <el-card>
          <template #header>允许关注的设备层级(未启用的层级不做趋势分析)</template>
          <el-table :data="profile.watch_configs || []" size="small">
            <el-table-column label="层级" width="110">
              <template #default="{ row }">
                {{ row.level === 'COMPONENT' ? '部件级' : '整车级' }}
              </template>
            </el-table-column>
            <el-table-column prop="component_type" label="部件类型" width="150">
              <template #default="{ row }">{{ row.component_type || '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                  {{ row.enabled ? '已启用' : '未启用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" min-width="200" />
          </el-table>
          <div class="actions">
            <el-button type="primary" size="small" @click="runAll">手动触发全量趋势运行</el-button>
            <span v-if="runMsg" class="ok">{{ runMsg }}</span>
          </div>
        </el-card>
        <el-card style="margin-top: 16px">
          <template #header>传感器校准状态(失效/过期期间报警资格降级或取消)</template>
          <el-table :data="profile.calibrations || []" size="small" max-height="300">
            <el-table-column prop="serial" label="设备" width="100" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="CALIB_TAG[row.status]" size="small">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="有效期" width="200">
              <template #default="{ row }">
                {{ row.valid_from.slice(0, 10) }} ~ {{ row.valid_to ? row.valid_to.slice(0, 10) : '至今' }}
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" min-width="160" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="14">
        <el-card>
          <template #header>部件安装/更换区间(更换后趋势重新起算)</template>
          <el-table :data="profile.installs || []" size="small">
            <el-table-column prop="train" label="列车" width="70" />
            <el-table-column prop="component_type" label="部件" width="140" />
            <el-table-column prop="serial" label="序列号" width="110" />
            <el-table-column label="安装周期" width="200">
              <template #default="{ row }">
                {{ row.installed_at.slice(0, 10) }} ~ {{ row.removed_at ? row.removed_at.slice(0, 10) : '在役' }}
              </template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.active ? 'success' : 'info'" size="small">
                  {{ row.active ? '在役' : '已退役' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" min-width="150" />
          </el-table>
        </el-card>
        <el-card style="margin-top: 16px">
          <template #header>
            <div class="bar">
              <span>维修与故障记录(晚到记录触发健康期重标)</span>
              <el-button size="small" type="primary" @click="dlg = true">登记记录</el-button>
            </div>
          </template>
          <el-table :data="profile.maintenance || []" size="small" max-height="300">
            <el-table-column label="列车" width="70">
              <template #default="{ row }">{{ trainOf(row.train_id) }}</template>
            </el-table-column>
            <el-table-column label="类型" width="70">
              <template #default="{ row }">
                <el-tag :type="MAINT_TYPE_TAG[row.type]" size="small">
                  {{ MAINT_TYPE_LABEL[row.type] }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="occurred_at" label="发生时刻" width="160">
              <template #default="{ row }">{{ row.occurred_at.slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column prop="recorded_at" label="登记时刻" width="160">
              <template #default="{ row }">{{ row.recorded_at.slice(0, 16) }}</template>
            </el-table-column>
            <el-table-column label="晚到" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.late" type="danger" size="small">晚到</el-tag>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" min-width="220" />
          </el-table>
          <div v-if="maintMsg" class="ok" style="margin-top: 8px">{{ maintMsg }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="dlg" title="登记维修/故障记录" width="480px">
      <el-form label-width="90px">
        <el-form-item label="列车">
          <el-select v-model="form.train_id" style="width: 100%">
            <el-option v-for="t in trains" :key="t.id" :label="t.code" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.type">
            <el-radio-button value="FAULT">故障</el-radio-button>
            <el-radio-button value="REPAIR">维修</el-radio-button>
            <el-radio-button value="OVERHAUL">检修</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="发生时刻">
          <el-date-picker v-model="form.occurred_at" type="datetime" style="width: 100%"
                          value-format="YYYY-MM-DDTHH:mm:ss" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-alert type="warning" :closable="false" show-icon
                  title="发生时刻明显早于登记时刻的记录视为晚到,将自动触发新一轮趋势运行(健康期重标);旧报警保留当时依据。" />
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" @click="submitMaint">登记</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'
import { CALIB_TAG, MAINT_TYPE_LABEL, MAINT_TYPE_TAG } from '../utils'

const profile = ref({})
const trains = ref([])
const dlg = ref(false)
const form = ref({ train_id: null, type: 'FAULT', occurred_at: '', description: '' })
const maintMsg = ref('')
const runMsg = ref('')

function trainOf(id) {
  const t = trains.value.find((x) => x.id === id)
  return t ? t.code : id
}

async function load() {
  profile.value = (await api.get('/health/profile')).data
  trains.value = (await api.get('/meta/catalog')).data.trains
}

async function submitMaint() {
  const r = await api.post('/health/maintenance', form.value)
  const n = r.data.triggered_trend_run_ids.length
  maintMsg.value = r.data.late
    ? `晚到记录已登记,已触发 ${n} 个新趋势运行(健康期重标),旧报警保留当时依据`
    : '记录已登记(非晚到,未触发重分析)'
  dlg.value = false
  await load()
}

async function runAll() {
  const r = await api.post('/health/trend-runs', {})
  runMsg.value = `已生成 ${r.data.trend_run_ids.length} 个趋势运行`
}

onMounted(load)
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.actions { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
.ok { color: #2b7a2b; font-size: 12px; }
</style>
