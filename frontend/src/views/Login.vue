<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2>轨道交通牵引能耗<br />运行事实与对标平台</h2>
      <el-form @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名(admin / viewer)" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码"
                    @keyup.enter="doLogin" />
        </el-form-item>
        <el-button type="primary" style="width: 100%" :loading="loading" @click="doLogin">
          登录
        </el-button>
      </el-form>
      <p class="tip">测试账号:admin/admin123(管理)、viewer/viewer123(只读)</p>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'

const username = ref('admin')
const password = ref('')
const loading = ref(false)
const router = useRouter()

async function doLogin() {
  loading.value = true
  try {
    const { data } = await api.post('/login', { username: username.value, password: password.value })
    localStorage.setItem('token', data.token)
    localStorage.setItem('username', data.username)
    router.push('/')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap { height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #0f2a4a, #1c5d8c); }
.login-card { width: 380px; }
h2 { text-align: center; color: #1c5d8c; font-size: 20px; }
.tip { color: #999; font-size: 12px; text-align: center; }
</style>
