import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: () => import('./views/Overview.vue'), meta: { title: '运行总览' } },
    { path: '/signals', component: () => import('./views/Signals.vue'), meta: { title: '信号对比' } },
    { path: '/trades', component: () => import('./views/Trades.vue'), meta: { title: '交易记录' } },
    { path: '/reports', component: () => import('./views/Reports.vue'), meta: { title: '交易日报' } },
    { path: '/backtests', component: () => import('./views/Backtests.vue'), meta: { title: '策略回测' } },
    { path: '/configuration', component: () => import('./views/Configuration.vue'), meta: { title: '参数配置' } },
  ],
})
