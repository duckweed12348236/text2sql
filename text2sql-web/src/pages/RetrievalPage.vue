<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import PageHeader from '@/components/PageHeader.vue'
import TurnCard from '@/components/TurnCard.vue'
import { listDatabases } from '@/api/modeling'
import { askQuestion, deleteSession, getSessionDetail, listSessions } from '@/api/retrieval'
import { RequestError, errorMessage } from '@/plugins/request'
import { loadActive, saveActive } from '@/composables/useConversations'
import type { ContextTable } from '@/types/dataset'
import type { Database } from '@/types/modeling'
import type {
  RetrievalSession,
  RetrievalStep,
  RetrievalToken,
  SessionTurn,
  TurnRecord,
} from '@/types/retrieval'

/**
 * 检索问答（主页）：一次提问跑一轮检索，链路上报以 SSE 逐帧回来。
 *
 * 会话以 `session_id` 为准：带回去即接回同一会话；反问帧（`clarification`）是中断，
 * 提交澄清后从停住处继续。历史轮次由会话详情接口（`GET /retrieval/session/{id}`）回显，
 * 正在跑的那一轮用 SSE 帧在本地拼出来，两者共用同一个轮次卡片。
 */

const databases = ref<Database[]>([])
const databaseId = ref('')
const draft = ref('')

const sessionId = ref('')
/** 轮次流容器：自己滚动，输入区不压在它上面 */
const feedRef = ref<HTMLElement | null>(null)
/** 已完成的轮次（按发生顺序，来自会话详情） */
const turns = ref<TurnRecord[]>([])
/** 正在跑的那一轮：与已完成轮次同构，跑完即并入 turns */
const live = ref<TurnRecord | null>(null)
const running = ref(false)
const loadingHistory = ref(false)
/** 会话累计 Token（各轮之和，来自会话详情） */
const sessionTokens = ref(0)

const sessions = ref<RetrievalSession[]>([])
const sessionsOpen = ref(false)
const loadingSessions = ref(false)
const deletingSessionId = ref('')

let controller: AbortController | null = null
let turnSeq = 0

const feed = computed<TurnRecord[]>(() =>
  live.value ? [...turns.value, live.value] : turns.value,
)

/** 等用户补充的那一轮：最后一轮以反问收尾，且当前没在跑 */
const pendingId = computed<string>(() => {
  if (running.value || !turns.value.length) return ''
  const last = turns.value[turns.value.length - 1]
  return last?.clarification ? last.id : ''
})

const canAsk = computed<boolean>(
  () => Boolean(databaseId.value) && Boolean(draft.value.trim()) && !running.value,
)

const currentDatabase = computed<Database | null>(
  () => databases.value.find(item => item.id === databaseId.value) ?? null,
)

/** 本轮 Token：优先正在跑的那一轮，其次最后一轮的累计值 */
const tokenUsage = computed<RetrievalToken | null>(() => {
  if (live.value?.token) return live.value.token
  for (let index = turns.value.length - 1; index >= 0; index -= 1) {
    const token = turns.value[index]?.token
    if (token) return token
  }
  return null
})

const tokenLabel = computed<string>(() =>
  tokenUsage.value ? `本轮 Token ${formatNumber(tokenUsage.value.total)}` : '',
)

const tokenTitle = computed<string>(() => {
  const parts: string[] = []
  const token = tokenUsage.value
  if (token && (token.prompt || token.completion)) {
    parts.push(`prompt ${formatNumber(token.prompt)} · completion ${formatNumber(token.completion)}`)
  }
  if (sessionTokens.value) parts.push(`会话累计 ${formatNumber(sessionTokens.value)}`)
  return parts.join(' · ')
})

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

/** 带了 session_id 但服务端查不到（已被删 / 编号失效）时返回 404 会话不存在 */
function isMissingSession(error: unknown): boolean {
  return (
    error instanceof RequestError &&
    error.status === 404 &&
    errorMessage(error).includes('会话不存在')
  )
}

function persistActive(): void {
  saveActive({ session_id: sessionId.value, database_id: databaseId.value })
}

/**
 * 新的一轮开始时把轮次流滚到末尾（尊重系统的减少动效设置）。
 * 轮次区自己滚动；窄屏回退成整页滚动时，改用 window。
 */
async function scrollToFeedEnd(): Promise<void> {
  await nextTick()
  const element = feedRef.value
  if (!element) return

  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const behavior: ScrollBehavior = reduce ? 'auto' : 'smooth'

  if (element.scrollHeight > element.clientHeight) {
    element.scrollTo({ top: element.scrollHeight, behavior })
    return
  }

  window.scrollTo({ top: document.body.scrollHeight, behavior })
}

/* ---------------- 会话详情 → 轮次 ---------------- */

function lastSql(steps: RetrievalStep[]): string {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const sql = steps[index]?.sql
    if (sql) return sql
  }
  return ''
}

function lastData(steps: RetrievalStep[]): unknown {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const data = steps[index]?.data
    if (data !== null && data !== undefined) return data
  }
  return null
}

function lastContexts(steps: RetrievalStep[]): ContextTable[] {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const contexts = steps[index]?.table_infos
    if (contexts?.length) return contexts
  }
  return []
}

function lastClarification(steps: RetrievalStep[]): string {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const clarification = steps[index]?.clarification
    if (clarification) return clarification
  }
  return ''
}

/** 失败原因：只认 `status` 为 `error` 且带 `error` 的帧（反问帧的 `error` 为空） */
function lastFailure(steps: RetrievalStep[]): string {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index]
    if (step?.status === 'error' && step.error) return step.error
  }
  return ''
}

/** 帧上的累计 Token；老数据只有轮次总量时退回只给总量 */
function lastToken(steps: RetrievalStep[], fallback: number): RetrievalToken | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const token = steps[index]?.token
    if (token) return token
  }
  return fallback > 0 ? { prompt: 0, completion: 0, total: fallback } : null
}

function toTurnRecord(turn: SessionTurn): TurnRecord {
  return {
    id: `turn-${turn.id}`,
    session_id: sessionId.value,
    content: turn.input,
    steps: turn.steps,
    sql: lastSql(turn.steps),
    data: lastData(turn.steps),
    contexts: lastContexts(turn.steps),
    failure: lastFailure(turn.steps),
    clarification: lastClarification(turn.steps),
    token: lastToken(turn.steps, turn.tokens),
    started_at: turn.created_at,
  }
}

/**
 * 拉会话详情回显轮次。
 * `mergeLocal`：刚跑完一轮时用——进度帧是整轮结束时写回的，服务端可能还少这一轮，
 * 就补上本地拼出来的尾部，别让刚看到的结果消失。
 */
async function loadHistory(mergeLocal = false): Promise<void> {
  if (!sessionId.value) {
    turns.value = []
    sessionTokens.value = 0
    return
  }

  // 刚跑完那一轮的后台刷新不弹遮罩，免得结果闪一下
  loadingHistory.value = !mergeLocal
  try {
    const detail = await getSessionDetail(sessionId.value)
    const serverTurns = detail.turns.map(toTurnRecord)

    if (mergeLocal && serverTurns.length < turns.value.length) {
      turns.value = [...serverTurns, ...turns.value.slice(serverTurns.length)]
    } else {
      turns.value = serverTurns
    }

    sessionTokens.value = detail.tokens
  } catch (error) {
    if (!mergeLocal) {
      turns.value = []
      sessionTokens.value = 0
    }
    ElMessage.error(errorMessage(error, '会话轮次加载失败'))
  } finally {
    loadingHistory.value = false
  }
}

/* ---------------- 提问 / 续跑 ---------------- */

/** 响应头里的会话编号先到，读第一帧之前就能拿到 */
function applySession(id: string): void {
  sessionId.value = id
}

function applyStep(step: RetrievalStep): void {
  const current = live.value
  if (!current) return

  current.steps.push(step)
  if (step.session_id) sessionId.value = step.session_id
  if (step.clarification) current.clarification = step.clarification
  if (step.sql) current.sql = step.sql
  if (step.table_infos?.length) current.contexts = step.table_infos
  if (step.token) current.token = step.token
  if (step.status === 'error' && step.error) current.failure = step.error
  if (step.data !== null && step.data !== undefined) current.data = step.data
}

/** 一轮跑完：先并进列表（取详情失败也不丢），再以服务端明细为准覆盖 */
function commitTurn(): void {
  const current = live.value
  if (!current) return

  turns.value = [...turns.value, { ...current, session_id: sessionId.value }]
  live.value = null
  void scrollToFeedEnd()
}

/** 提一个问题 / 提交一次澄清；返回这一轮是否没有失败 */
async function ask(content: string): Promise<boolean> {
  if (!databaseId.value) {
    ElMessage.warning('请先选择业务库')
    return false
  }

  const text = content.trim()
  if (!text) {
    ElMessage.warning('请先写清楚要查什么')
    return false
  }

  if (running.value) return false

  turnSeq += 1
  const startedAt = new Date()
  live.value = {
    id: `local-${startedAt.getTime()}-${turnSeq}`,
    session_id: sessionId.value,
    content: text,
    steps: [],
    sql: '',
    data: null,
    contexts: [],
    failure: '',
    clarification: '',
    token: null,
    started_at: startedAt.toISOString(),
  }
  running.value = true
  void scrollToFeedEnd()

  const current = new AbortController()
  controller = current
  let failed = false

  try {
    await askQuestion(
      { content: text, database_id: databaseId.value, session_id: sessionId.value || null },
      applyStep,
      { signal: current.signal, onSession: applySession },
    )
  } catch (error) {
    failed = true
    if (!live.value) return false

    if (isMissingSession(error)) {
      // 会话被删或编号失效：清掉会话，下一轮就是新对话
      sessionId.value = ''
      turns.value = []
      sessionTokens.value = 0
      live.value.failure = '这个会话在服务端已不存在（可能已被删除），已切到新对话，请重新提问。'
    } else {
      live.value.failure = isAbort(error) ? '已中断本轮检索' : errorMessage(error, '检索失败')
    }
  } finally {
    running.value = false
    controller = null
    commitTurn()
    await loadHistory(true)
    if (sessionsOpen.value) void loadSessions()
  }

  return !failed && !turns.value[turns.value.length - 1]?.failure
}

async function submitDraft(): Promise<void> {
  const ok = await ask(draft.value)
  if (ok) draft.value = ''
}

/** 待补充那一轮提交的澄清 */
async function answerClarification(text: string): Promise<void> {
  await ask(text)
}

function stop(): void {
  controller?.abort()
}

/** 新对话：清掉会话编号与已回显的轮次，下一次提交由服务端新建会话 */
function startNewSession(): void {
  stop()
  live.value = null
  sessionId.value = ''
  turns.value = []
  sessionTokens.value = 0
  draft.value = ''
  persistActive()
}

async function copyText(text: string, tip: string): Promise<void> {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(tip)
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}

async function loadSessions(): Promise<void> {
  loadingSessions.value = true
  try {
    sessions.value = await listSessions()
  } catch (error) {
    sessions.value = []
    ElMessage.error(errorMessage(error, '会话列表加载失败'))
  } finally {
    loadingSessions.value = false
  }
}

function openSessions(): void {
  sessionsOpen.value = true
  void loadSessions()
}

/** 接回某个会话：轮次从会话详情取回来，之后提问带它的编号 */
async function resumeSession(item: RetrievalSession): Promise<void> {
  stop()
  live.value = null
  sessionId.value = item.id
  turns.value = []
  sessionTokens.value = 0
  sessionsOpen.value = false
  persistActive()

  await loadHistory()
  void scrollToFeedEnd()
  if (turns.value.length) ElMessage.success(`已接回会话，回显 ${turns.value.length} 轮`)
  else ElMessage.info('已接回这个会话（服务端还没有轮次记录），接着提问即可')
}

async function removeSession(item: RetrievalSession): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '先清检索记忆、后删登记：记忆清不干净时接口返回 503、登记原样保留，可以重试；名下的轮次随外键一起删，回显的对话也会一起消失。',
      `删除会话「${item.id}」`,
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return
  }

  deletingSessionId.value = item.id
  try {
    await deleteSession(item.id)
    ElMessage.success('会话已删除')
    if (sessionId.value === item.id) {
      live.value = null
      sessionId.value = ''
      turns.value = []
      sessionTokens.value = 0
      persistActive()
    }
    await loadSessions()
  } catch (error) {
    ElMessage.error(errorMessage(error, '会话删除失败'))
  } finally {
    deletingSessionId.value = ''
  }
}

async function loadDatabases(): Promise<void> {
  try {
    databases.value = await listDatabases()
  } catch (error) {
    databases.value = []
    ElMessage.error(errorMessage(error, '业务库列表加载失败'))
  }
}

// 换库或换会话都记下来，刷新后回到原来的位置
watch([sessionId, databaseId], persistActive)

onMounted(async () => {
  await loadDatabases()

  const active = loadActive()
  const preferred =
    databases.value.find(item => item.id === active.database_id) ?? databases.value[0] ?? null
  databaseId.value = preferred ? preferred.id : ''
  sessionId.value = active.session_id
  await loadHistory()
  void scrollToFeedEnd()
})
</script>

<template>
  <div class="page">
    <PageHeader
      title="检索问答"
      description="把自然语言问题转成 SQL 并在业务库上执行，链路进度以 SSE 逐帧回来。只在该库的向量集合里召回，不跨库；会话以 session_id 为准，带回去即接回同一会话。链路会先改写问题、必要时补齐缺的表与字段再生成 SQL；问题过于模糊或指标口径对不上时会先反问，补充后按同一会话继续。"
    >
      <template #actions>
        <el-button @click="openSessions">
          <AppIcon name="clock" :size="16" />
          <span>会话</span>
        </el-button>
        <el-button :disabled="running" @click="startNewSession">
          <AppIcon name="plus" :size="16" />
          <span>新会话</span>
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

      <span class="toolbar__spacer" />

      <span v-if="tokenUsage" class="muted-12" :title="tokenTitle">{{ tokenLabel }}</span>
      <template v-if="sessionId">
        <span class="mono" :title="sessionId">会话 {{ sessionId }}</span>
        <el-button link type="primary" @click="copyText(sessionId, '会话编号已复制')">
          <AppIcon name="copy" :size="14" />
          <span>复制</span>
        </el-button>
      </template>
      <span v-else class="muted-12">新对话（提交后由服务端新建会话）</span>
    </div>

    <div ref="feedRef" v-loading="loadingHistory" class="feed">
      <TurnCard
        v-for="(turn, index) in feed"
        :key="turn.id"
        :turn="turn"
        :round="index + 1"
        :pending="turn.id === pendingId"
        :running="running && turn.id === live?.id"
        @answer="answerClarification"
      />

      <div v-if="!feed.length && !loadingHistory" class="feed__empty">
        <h2 class="feed__empty-title">还没有轮次</h2>
        <p class="muted-12 feed__empty-text">
          写下第一个问题，链路会先改写问题、召回字段，再生成并执行 SQL；问题过于模糊或指标口径对不上时会先反问，
          补充后从停住的地方继续。每一轮都会落到会话里，切回会话或刷新页面都能回显。
        </p>
      </div>
    </div>

    <div class="composer">
      <el-input
        v-model="draft"
        type="textarea"
        :rows="3"
        resize="none"
        :disabled="running"
        :placeholder="
          pendingId
            ? '上一轮在反问处停住：可以在上面直接填澄清，也可以在这里接着说'
            : '用自然语言描述要查什么，如：上个月华东区的订单总额是多少？'
        "
        @keydown.ctrl.enter="submitDraft"
        @keydown.meta.enter="submitDraft"
      />
      <div class="composer__foot">
        <span class="muted-12">
          Ctrl / ⌘ + Enter 提交；检索只读，不会改动业务库
          <template v-if="pendingId">· 当前有一轮等待补充</template>
        </span>
        <el-button v-if="running" @click="stop">中断</el-button>
        <el-button v-else type="primary" :disabled="!canAsk" @click="submitDraft">
          <AppIcon name="ask" :size="16" />
          <span>{{ pendingId ? '继续提问' : '提问' }}</span>
        </el-button>
      </div>
    </div>

    <el-drawer v-model="sessionsOpen" title="会话" size="520px">
      <div class="session-panel">
        <div class="session-panel__head">
          <span class="muted-12">
            按会话编号倒序，新的在前。列表只带轮次数；「接着聊」会取会话详情，把每一轮的问题、反问与结果都拉回来。
          </span>
          <el-button link type="primary" :loading="loadingSessions" @click="loadSessions">
            刷新
          </el-button>
        </div>

        <el-empty
          v-if="!sessions.length && !loadingSessions"
          class="empty-cell"
          description="还没有会话，提一个问题就会留下一条"
          :image-size="96"
        />

        <ul v-else v-loading="loadingSessions" class="session-list">
          <li
            v-for="item in sessions"
            :key="item.id"
            class="session-item"
            :class="{ 'session-item--active': item.id === sessionId }"
          >
            <div class="session-item__main">
              <span class="mono session-item__id" :title="item.id">{{ item.id }}</span>
              <div class="session-item__meta">
                <span class="tag tag--neutral">{{ item.turns }} 轮</span>
                <span v-if="item.id === sessionId" class="tag tag--sky">当前</span>
                <span class="muted-12">最近一轮 {{ formatTime(item.last_turn_at) }}</span>
              </div>
            </div>
            <div class="session-item__actions">
              <el-button
                link
                type="primary"
                :disabled="item.id === sessionId"
                @click="resumeSession(item)"
              >
                接着聊
              </el-button>
              <el-button
                link
                type="danger"
                :loading="deletingSessionId === item.id"
                @click="removeSession(item)"
              >
                删除
              </el-button>
            </div>
          </li>
        </ul>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 页面定高，轮次区自己滚动：输入区在流内固定在底部，不会被长轮次压住 */
.page {
  height: calc(100vh - var(--topbar-height));
  min-height: 0;
  padding-bottom: 16px;
  overflow: hidden;
}

.toolbar {
  flex: none;
}

.db-select {
  width: 240px;
}

.toolbar__spacer {
  flex: 1;
}

/* 轮次流：用细分隔线分区，不再包卡片；空态与输入区各占一段 */
.feed {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

.feed__empty {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 48px 0;
  border-bottom: 1px solid var(--hairline);
}

.feed__empty-title {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.01em;
}

.feed__empty-text {
  margin: 0;
  max-width: 680px;
  line-height: 1.7;
}

.composer {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  background: var(--canvas);
  border-top: 1px solid var(--hairline);
}

.composer__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.session-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.session-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.session-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  transition: background-color 160ms ease;
}

.session-item:hover {
  background: var(--surface-soft);
}

.session-item--active {
  background: var(--tint-lavender);
  border-color: transparent;
}

.session-item__main {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.session-item__id {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.session-item__actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 4px;
}

/* 窄屏放不下定高布局：回到整页滚动，轮次区不再单独滚 */
@media (max-width: 900px) {
  .page {
    height: auto;
    min-height: calc(100vh - var(--topbar-height));
    overflow: visible;
  }

  .feed {
    overflow: visible;
  }
}
</style>
