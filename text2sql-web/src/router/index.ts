import { createRouter, createWebHistory } from 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 顶栏与侧栏展示的模块名 */
    title: string
    /** 页面标题下的说明 */
    subtitle?: string
  }
}

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: { name: 'database' } },
    {
      path: '/database',
      name: 'database',
      component: () => import('@/pages/DatabasePage.vue'),
      meta: { title: '业务库', subtitle: '已登记的业务数据库' },
    },
    {
      path: '/table',
      name: 'table',
      component: () => import('@/pages/TablePage.vue'),
      meta: { title: '表与字段', subtitle: '表与字段的元数据登记' },
    },
    {
      path: '/metric',
      name: 'metric',
      component: () => import('@/pages/MetricPage.vue'),
      meta: { title: '指标', subtitle: '跨库使用的指标定义' },
    },
    {
      path: '/dataset',
      name: 'dataset',
      component: () => import('@/pages/EvaluationPage.vue'),
      meta: { title: '数据集', subtitle: '评测数据集的出题与评估' },
    },
    {
      // 路径不能与后端接口前缀同名：/retrieval 会被代理转发到后端，
      // 浏览器刷新这个地址就会拿到 405，所以这里用 /ask
      path: '/ask',
      name: 'retrieval',
      component: () => import('@/pages/RetrievalPage.vue'),
      meta: { title: '检索问答', subtitle: '自然语言问题 → SQL 并执行' },
    },
    { path: '/:pathMatch(.*)*', redirect: { name: 'database' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

export default router
