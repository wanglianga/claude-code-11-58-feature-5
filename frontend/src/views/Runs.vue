<template>
  <el-card>
    <template #header>
      <div class="bar">
        <span>运行列表(迟到补传产生新版本,旧版本保留)</span>
        <el-checkbox v-model="allVersions" @change="load">显示全部版本</el-checkbox>
      </div>
    </template>
    <el-table :data="rows" size="small" @row-click="(r) => $router.push(`/runs/${r.id}`)">
      <el-table-column prop="run_key" label="运行" width="110" />
      <el-table-column label="版本" width="80">
        <template #default="{ row }">
          <el-tag :type="row.version > 1 ? 'warning' : 'info'" size="small">v{{ row.version }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="train" label="列车" width="80" />
      <el-table-column prop="vehicle_model" label="车型" width="80" />
      <el-table-column prop="line" label="线路" width="70" />
      <el-table-column prop="driver_mode" label="司机模式" width="90" />
      <el-table-column prop="started_at" label="开始" width="170" />
      <el-table-column prop="ended_at" label="结束" width="170" />
      <el-table-column prop="distance_km" label="里程km" width="80" />
      <el-table-column prop="passengers" label="载客" width="80" />
      <el-table-column prop="shift" label="班次" width="100" />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'PUBLISHED' ? 'success' : 'info'" size="small">
            {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const rows = ref([])
const allVersions = ref(false)
async function load() {
  rows.value = (await api.get('/runs', { params: { all_versions: allVersions.value } })).data
}
onMounted(load)
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
:deep(.el-table__row) { cursor: pointer; }
</style>
