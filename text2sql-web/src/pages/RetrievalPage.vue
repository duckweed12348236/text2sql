<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import ContextTables from '@/components/ContextTables.vue'
import PageHeader from '@/components/PageHeader.vue'
import StepTimeline from '@/components/StepTimeline.vue'
import { listDatabases } from '@/api/modeling'
import { askQuestion } from '@/api/retrieval'
import { errorMessage } from '@/plugins/request'
import type { ContextTable, Step } from '@/types/dataset'
import type { Database } from '@/types/modeling'
import type { RetrievalStep } from '@/types/retrieval'

/**
 * 检索问答：一次提问跑一轮检索（问题 → SQL → 执行），链路上报以 SSE 逐帧回来。
 * 与出题 / 评测不同，这条路不落任务记录，流跑完即结束。
 */

const databases = ref<Database[]>([])
const databaseId = ref('')
const question = ref('')

const running = ref(false)
const steps = ref<RetrievalStep[]>([])
const failure = ref('')
/** 最终 SQL：执行完成帧上那条才是准的，之前的只是候选 */
const sql = ref('')
const rows = ref<Record<string, unknown>[]>([])
/** 结果不是「对象数组」时按 JSON 原样展示 */
const rawData = ref('')
const contexts = ref<ContextTable[]>([])

let controller: AbortController | null = null

const currentDatabase = computed<Database | null>(
  () => databases.value.find(item => item.id === databaseId.value) ?? null,
)

const canAsk = computed<boolean>(
  () => Boolean(databaseId.value) && Boolean(question.value.trim()) && !running.value,
)

const columns = computed<string[]>(() => {
  const first = rows.value[0]
  return first ? Object.keys(first) : []
})

/** 检索帧的失败状态叫 `error`，时间线组件按 `failed` 渲染 */
const timeline = computed<Step[]>(() =>
  steps.value.map(item => ({
    message: item.message,
    status: item.status === 'error' ? 'failed' : item.status,
    error: item.error,
    dataset_id: null,
  })),
)

const hasResult = computed<boolean>(
  () => Boolean(sql.value) || rows.value.length > 0 || Boolean(rawData.value),
)

function isRow(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value) ?? ''
  return String(value)
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function reset(): void {
  steps.value = []
  failure.value = ''
  sql.value = ''
  rows.value = []
  rawData.value = ''
  contexts.value = []
}

function applyData(data: unknown): void {
  // 结果是「一行行记录」时铺成表格，其余形态按 JSON 展示
  if (Array.isArray(data) && data.every(isRow)) {
    rows.value = data
    rawData.value = ''
    return
  }

  rows.value = []
  rawData.value = JSON.stringify(data, null, 2) ?? String(data)
}

function applyStep(step: RetrievalStep): void {
  steps.value.push(step)

  if (step.sql) sql.value = step.sql
  if (step.table_infos?.length) contexts.value = step.table_infos
  if (step.status === 'error' && step.error) failure.value = step.error
  if (step.data !== null && step.data !== undefined) applyData(step.data)
}

async function start(): Promise<void> {
  if (!databaseId.value) {
    ElMessage.warning('请先选择业务库')
    return
  }

  const content = question.value.trim()
  if (!content) {
    ElMessage.warning('请先写清楚要查什么')
    return
  }

  reset()
  running.value = true
  const current = new AbortController()
  controller = current

  try {
    await askQuestion({ content, database_id: databaseId.value }, applyStep, current.signal)
  } catch (error) {
    failure.value = isAbort(error) ? '已中断本轮检索' : errorMessage(error, '检索失败')
  } finally {
    running.value = false
    controller = null
  }
}

function stop(): void {
  controller?.abort()
}

function clear(): void {
  stop()
  question.value = ''
  reset()
}

async function copySql(): Promise<void> {
  if (!sql.value) return
  try {
    await navigator.clipboard.writeText(sql.value)
    ElMessage.success('SQL 已复制')
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}

async function load(): Promise<void> {
  try {
    databases.value = await listDatabases()
    if (!databases.value.some(item => item.id === databaseId.value)) {
      databaseId.value = databases.value[0]?.id ?? ''
    }
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader
      title="检索问答"
      description="把自然语言问题转成 SQL 并在业务库上执行，链路进度以 SSE 逐帧回来。只在该库的向量集合里召回，不跨库；库还没采集过时召回为空，会以「没有候选表，无法生成 SQL」之类的错误帧收场。"
    >
      <template #actions>
        <el-button :disabled="running" @click="clear">
          <AppIcon name="trash" :size="16" />
          <span>清空</span>
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-select
        v-model="databaseId"
        class="db-select"
        placeholder="选择业务库"
        filterable
        :disabled="running || !databases.length"
      >
        <el-option
          v-for="item in databases"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        />
      </el-select>
      <span class="muted-12">共 {{ databases.length }} 个业务库</span>
      <span v-if="currentDatabase && !currentDatabase.collected" class="muted-12">
        · 该库还没采集过，召回多半为空
      </span>
      <span v-else-if="!databases.length" class="muted-12">· 未取到业务库，检索暂不可用</span>
    </div>

    <div class="ask">
      <el-input
        v-model="question"
        type="textarea"
        :rows="3"
        resize="none"
        :disabled="running"
        placeholder="用自然语言描述要查什么，如：上个月华东区的订单总额是多少？"
        @keydown.ctrl.enter="start"
        @keydown.meta.enter="start"
      />
      <div class="ask__foot">
        <span class="muted-12">Ctrl / ⌘ + Enter 提交；检索只读，不会改动业务库</span>
        <el-button v-if="running" @click="stop">中断</el-button>
        <el-button v-else type="primary" :disabled="!canAsk" @click="start">
          <AppIcon name="ask" :size="16" />
          <span>提问</span>
        </el-button>
      </div>
    </div>

    <div class="split">
      <section class="panel">
        <header class="panel__head">
          <span class="panel__title">链路上报</span>
          <span class="muted-12">{{ steps.length }} 帧</span>
        </header>
        <div class="panel__body">
          <StepTimeline v-if="steps.length" :steps="timeline" :running="running" />
          <p v-else class="muted-12">还没有上报：写完问题点「提问」，这里逐帧显示意图识别、召回、生成与执行的进度。</p>
        </div>
      </section>

      <section class="panel">
        <header class="panel__head">
          <span class="panel__title">结果</span>
          <span v-if="rows.length" class="muted-12">{{ rows.length }} 行</span>
        </header>
        <div class="panel__body">
          <p v-if="failure" class="alert alert--danger">{{ failure }}</p>

          <template v-if="sql">
            <div class="sub-head">
              <span class="panel__title">SQL</span>
              <el-button link type="primary" @click="copySql">
                <AppIcon name="copy" :size="14" />
                <span>复制</span>
              </el-button>
            </div>
            <code class="sql">{{ sql }}</code>
          </template>

          <template v-if="rows.length || rawData">
            <div class="sub-head">
              <span class="panel__title">查询结果</span>
            </div>

            <div v-if="rows.length" class="table-wrap">
              <el-table :data="rows" max-height="360" size="small">
                <el-table-column type="index" label="#" width="56" />
                <el-table-column
                  v-for="column in columns"
                  :key="column"
                  :label="column"
                  min-width="140"
                  show-overflow-tooltip
                >
                  <template #default="{ row }">
                    <span class="mono">{{ formatCell(row[column]) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <pre v-else class="raw">{{ rawData }}</pre>
          </template>

          <template v-if="contexts.length">
            <div class="sub-head">
              <span class="panel__title">检索到的表</span>
              <span class="muted-12">{{ contexts.length }} 张</span>
            </div>
            <ContextTables :contexts="contexts" default-open />
          </template>

          <p v-if="!hasResult && !failure" class="muted-12">
            跑完一轮后这里给出最终 SQL、查询结果，以及用于生成这条 SQL 的表 / 字段 / 取值。
          </p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.db-select {
  width: 240px;
}

.ask {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 24px;
}

.ask__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.split {
  display: grid;
  flex: 1;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr);
  gap: 24px;
  min-height: 360px;
}

.panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.panel__head {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--hairline);
  background: var(--surface-soft);
}

.panel__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
}

.panel__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
}

.sub-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 20px 0 8px;
}

.sub-head:first-child {
  margin-top: 0;
}

.sql {
  display: block;
  padding: 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.raw {
  margin: 0;
  padding: 12px;
  border-radius: var(--radius-lg);
  background: var(--surface);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.alert {
  display: block;
  margin: 0 0 16px;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  font-size: 13px;
}

.alert--danger {
  background: #fdeaea;
  color: var(--danger);
}

@media (max-width: 1024px) {
  .split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
