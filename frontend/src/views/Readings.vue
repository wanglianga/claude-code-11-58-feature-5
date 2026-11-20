<template>
  <div>
    <el-card>
      <template #header>
        <div class="bar">
          <span>原始累计值采样(含设备序列、通信状态、计量倍率)</span>
          <el-select v-model="deviceId" placeholder="全部设备" clearable style="width: 220px"
                     @change="load">
            <el-option v-for="d in devices" :key="d.id"
                       :label="`${d.serial}(${d.kind === 'ONBOARD' ? '车载' : '变电所'})`"
                       :value="d.id" />
          </el-select>
        </div>
      </template>
      <el-table :data="rows" size="small" max-height="640">
        <el-table-column prop="serial" label="设备序列" width="110" />
        <el-table-column prop="sampled_at" label="采样时刻" width="170" />
        <el-table-column prop="received_at" label="到达时刻" width="170" />
        <el-table-column prop="seq" label="序列" width="70" />
        <el-table-column label="牵引/受电" width="100">
          <template #default="{ row }">{{ row.traction_cum ?? row.import_cum ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="回馈/返送" width="100">
          <template #default="{ row }">{{ row.regen_cum ?? row.export_cum ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="aux_cum" label="辅助" width="90">
          <template #default="{ row }">{{ row.aux_cum ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="multiplier" label="倍率" width="70" />
        <el-table-column label="通信" width="90">
          <template #default="{ row }">
            <el-tag :type="row.comm_status === 'OK' ? 'success' : 'danger'" size="small">
              {{ row.comm_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="迟到" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.late" type="warning" size="small">迟到</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const rows = ref([])
const devices = ref([])
const deviceId = ref(null)

async function load() {
  const params = deviceId.value ? { device_id: deviceId.value } : {}
  rows.value = (await api.get('/readings', { params: { ...params, limit: 300 } })).data
}
onMounted(async () => {
  devices.value = (await api.get('/devices')).data
  await load()
})
</script>

<style scoped>
.bar { display: flex; justify-content: space-between; align-items: center; }
</style>
