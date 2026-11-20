<template>
  <el-card>
    <template #header>
      <div class="bar">
        <span>已发布指标(迟到数据只产生新版本,旧水位保留)</span>
        <div>
          <el-select v-model="scope" placeholder="全部范围" clearable style="width: 130px"
                     @change="load">
            <el-option v-for="(l, k) in SCOPE_LABEL" :key="k" :label="l" :value="k" />
          </el-select>
          <el-input v-model="scopeKey" placeholder="范围键(如 RUN-1001)" clearable
                    style="width: 180px; margin-left: 8px" @change="load" />
        </div>
      </div>
    </template>
    <el-table :data="rows" size="small" max-height="640">
      <el-table-column label="范围" width="90">
        <template #default="{ row }">{{ SCOPE_LABEL[row.scope] }}</template>
      </el-table-column>
      <el-table-column prop="scope_key" label="对象" width="120" />
      <el-table-column label="指标" width="180">
        <template #default="{ row }">{{ METRIC_LABEL[row.name] || row.name }}</template>
      </el-table-column>
      <el-table-column label="值" width="110">
        <template #default="{ row }">
          <b :class="{ unknown: row.value === null }">{{ fmtVal(row.value, 4) }}</b>
        </template>
      </el-table-column>
      <el-table-column label="来源覆盖" width="100">
        <template #default="{ row }">{{ (row.coverage * 100).toFixed(0) }}%</template>
      </el-table-column>
      <el-table-column label="运行版本" width="90">
        <template #default="{ row }">v{{ row.run_version }}</template>
      </el-table-column>
      <el-table-column prop="published_at" label="发布时间" width="170" />
      <el-table-column label="差异说明" min-width="220">
        <template #default="{ row }">
          <span v-if="row.diff_note" class="note">{{ row.diff_note }}</span>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="复盘" width="80">
        <template #default="{ row }">
          <el-button link type="primary" @click="$router.push(`/replay?metric=${row.id}`)">
            复盘
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'
import { METRIC_LABEL, SCOPE_LABEL, fmtVal } from '../utils'

const rows = ref([])
const scope = ref(null)
const scopeKey = ref('')

async function load() {
  const params = {}
  if (scope.value) params.scope = scope.value
  if (scopeKey.value) params.scope_key = scopeKey.value
  rows.value = (await api.get('/metrics', { params })).data
}
onMounted(load)
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
.unknown { color: #c45656; }
.note { color: #b88230; font-size: 12px; }
</style>
