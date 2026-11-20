<template>
  <el-container class="layout">
    <el-aside width="230px">
      <div class="logo">牵引能耗对标平台</div>
      <el-menu :default-active="$route.path" router background-color="#0f2a4a"
               text-color="#b8c7d9" active-text-color="#fff">
        <el-menu-item index="/">总览</el-menu-item>
        <el-menu-item index="/readings">采集数据</el-menu-item>
        <el-menu-item index="/runs">运行与能量</el-menu-item>
        <el-menu-item index="/metrics">指标服务</el-menu-item>
        <el-menu-item index="/replay">复盘回查</el-menu-item>
        <el-menu-item index="/health">健康档案</el-menu-item>
        <el-menu-item index="/health/trends">残差趋势</el-menu-item>
        <el-menu-item index="/health/alerts">健康报警</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header>
        <span />
        <span>
          {{ username }}
          <el-button link type="primary" @click="logout">退出</el-button>
        </span>
      </el-header>
      <el-main><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const username = computed(() => localStorage.getItem('username') || '')
function logout() {
  localStorage.removeItem('token')
  router.push('/login')
}
</script>

<style scoped>
.layout { height: 100vh; }
.el-aside { background: #0f2a4a; }
.logo { color: #fff; font-weight: 600; padding: 18px 16px; font-size: 15px; }
.el-menu { border-right: none; }
.el-header { display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid #eee; }
</style>
