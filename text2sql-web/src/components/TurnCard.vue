<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AppIcon from '@/components/AppIcon.vue'
import ContextTables from '@/components/ContextTables.vue'
import StepTimeline from '@/components/StepTimeline.vue'
import type { Step } from '@/types/dataset'
import type { TurnRecord } from '@/types/retrieval'

/**
 * 一轮问答：问题 → 进度帧（可展开）→ 反问 / 失败 / SQL 与结果 → Token。
 * 以「待补充」收尾的那一轮会带上澄清输入框，提交后由页面按同一会话续跑。
 */

const props = defineProps<{
  turn: TurnRecord
  /** 轮次序号（同一会话内从 1 开始） */
  round?: number
  /** 该轮以反问收尾、正在等用户补充 */
  pending?: boolean
  /** 这一轮还在跑 */
  running?: boolean
}>()

const emit = defineEmits<{ answer: [text: string] }>()

/** 待补充时的回答输入 */
const reply = ref('')
const showSteps = ref(Boolean(props.running))

const timeline = computed<Step[]>(() =>
  props.turn.steps.map(step => ({
    message: step.message,
    status: step.status === 'error' ? 'failed' : step.status,
    error: step.error,
    dataset_id: null,
  })),
)

const rows = computed<Record<string, unknown>[]>(() => {
  const data = props.turn.data
  if (!Array.isArray(data) || !data.length) return []
  if (!data.every(item => item !== null && typeof item === 'object' && !Array.isArray(item))) {
    return []
  }
  return data as Record<string, unknown>[]
})

const columns = computed<string[]>(() => {
  const first = rows.value[0]
  return first ? Object.keys(first) : []
})

/** 不是「一行行记录」时按 JSON 原样展示 */
const rawData = computed<string>(() => {
  const data = props.turn.data
  if (data === null || data === undefined) return ''
  if (rows.value.length) return ''
  return JSON.stringify(data, null, 2) ?? String(data)
})

const statusLabel = computed<string>(() => {
  if (props.running) return '进行中'
  if (props.turn.clarification) return '待补充'
  if (props.turn.failure) return '失败'
  return '完成'
})

const statusTag = computed<string>(() => {
  if (props.running) return 'tag--sky'
  if (props.turn.clarification) return 'tag--peach'
  if (props.turn.failure) return 'tag--danger'
  return 'tag--mint'
})

const startAt = computed<string>(() => {
  const date = new Date(props.turn.started_at)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString('zh-CN', { hour12: false })
})

/** 老数据只留轮次总量时不含 prompt / completion，标题里就不写这两个数 */
const tokenHint = computed<string>(() => {
  const token = props.turn.token
  if (!token) return ''
  if (!token.prompt && !token.completion) return `本轮共 ${formatNumber(token.total)} Token`
  return `prompt ${formatNumber(token.prompt)} · completion ${formatNumber(token.completion)}`
})

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value) ?? ''
  return String(value)
}

function toggleSteps(): void {
  showSteps.value = !showSteps.value
}

function submitReply(): void {
  const text = reply.value.trim()
  if (!text || props.running) return
  emit('answer', text)
  reply.value = ''
}

async function copySql(): Promise<void> {
  if (!props.turn.sql) return
  try {
    await navigator.clipboard.writeText(props.turn.sql)
    ElMessage.success('SQL 已复制')
  } catch {
    ElMessage.warning('当前浏览器不允许访问剪贴板')
  }
}
</script>

<template>
  <article class="turn" :class="{ 'turn--pending': pending, 'turn--live': running }">
    <header class="turn__head">
      <span class="turn__mark">Q</span>
      <p class="turn__question">{{ turn.content }}</p>
      <span v-if="round" class="muted-12 turn__round">#{{ round }}</span>
      <span class="tag" :class="statusTag">{{ statusLabel }}</span>
      <span class="muted-12 turn__time">{{ startAt }}</span>
    </header>

    <p v-if="turn.failure" class="alert alert--danger">{{ turn.failure }}</p>

    <div v-if="turn.clarification" class="clarify">
      <div class="clarify__head">
        <span class="tag tag--peach">反问</span>
        <span class="muted-12">
          {{ pending ? '补充后按同一会话继续' : '这一轮停在这里，可在下方继续提问' }}
        </span>
      </div>

      <p class="clarify__text">{{ turn.clarification }}</p>

      <template v-if="pending">
        <el-input
          v-model="reply"
          type="textarea"
          :rows="2"
          resize="none"
          :disabled="running"
          placeholder="针对上面的反问补充，如：口径按已支付订单算，销售额扣退款"
          @keydown.ctrl.enter="submitReply"
          @keydown.meta.enter="submitReply"
        />
        <div class="clarify__foot">
          <span class="muted-12">Ctrl / ⌘ + Enter 提交</span>
          <el-button type="primary" :disabled="!reply.trim() || running" @click="submitReply">
            提交澄清
          </el-button>
        </div>
      </template>
    </div>

    <div v-if="turn.steps.length" class="turn__steps">
      <el-button link type="primary" @click="toggleSteps">
        {{ showSteps ? '收起进度' : `展开进度 ${turn.steps.length} 帧` }}
      </el-button>
      <StepTimeline v-if="showSteps" :steps="timeline" :running="running" />
    </div>

    <template v-if="turn.sql">
      <div class="sub-head">
        <span class="sub-head__title">SQL</span>
        <el-button link type="primary" @click="copySql">
          <AppIcon name="copy" :size="14" />
          <span>复制</span>
        </el-button>
      </div>
      <code class="sql">{{ turn.sql }}</code>
    </template>

    <template v-if="rows.length || rawData">
      <div class="sub-head">
        <span class="sub-head__title">查询结果</span>
        <span v-if="rows.length" class="muted-12">{{ rows.length }} 行</span>
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

    <template v-if="turn.contexts.length">
      <div class="sub-head">
        <span class="sub-head__title">检索到的表</span>
        <span class="muted-12">{{ turn.contexts.length }} 张</span>
      </div>
      <ContextTables :contexts="turn.contexts" />
    </template>

    <footer v-if="turn.token" class="turn__foot">
      <span class="muted-12" :title="tokenHint">Token {{ formatNumber(turn.token.total) }}</span>
    </footer>
  </article>
</template>

<style scoped>
.turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 20px 0;
  border-bottom: 1px solid var(--hairline);
}

.turn:last-child {
  border-bottom: none;
}

.turn__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.turn__mark {
  display: grid;
  flex: none;
  place-items: center;
  width: 22px;
  height: 22px;
  margin-top: 1px;
  border-radius: var(--radius-md);
  background: var(--tint-lavender);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
}

.turn__question {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  line-height: 1.5;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.turn__round,
.turn__time {
  flex: none;
}

/* 反问：用警告色块区分「不是失败，也不是结果」 */
.clarify {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 16px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface-soft);
}

.clarify__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.clarify__text {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  color: var(--ink);
}

.clarify__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.turn__steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sub-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}

.sub-head__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  letter-spacing: 0.02em;
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
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  font-size: 13px;
}

.alert--danger {
  background: #fdeaea;
  color: var(--danger);
}

.turn__foot {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
