import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', component: () => import('./views/Dashboard.vue') },
      { path: 'readings', component: () => import('./views/Readings.vue') },
      { path: 'runs', component: () => import('./views/Runs.vue') },
      { path: 'runs/:id', component: () => import('./views/RunDetail.vue') },
      { path: 'metrics', component: () => import('./views/Metrics.vue') },
      { path: 'replay', component: () => import('./views/Replay.vue') },
      { path: 'health', component: () => import('./views/HealthProfile.vue') },
      { path: 'health/trends', component: () => import('./views/HealthTrends.vue') },
      { path: 'health/alerts', component: () => import('./views/HealthAlerts.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) return '/login'
})
export default router
