<script setup lang="ts">
import { useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StepTimeline from '@/components/StepTimeline.vue'
import { useTasks } from '@/composables/useTasks'
import type { TaskSnapshot, TaskStatus, TaskType } from '@/types/dataset'

/**
 * 后台任务面板：一个「任务」按钮 + 一个抽屉。
 *
 * 出题、评测与整库采集都是后台任务，投递即返回，进度统一收在这里：
 * 一次列出当前用户最近的一批任务，展开看每个任务的进度帧与产出。
 */

const router = useRouter()

const { tasks, loading, error, openNames, panelOpen, runningCount, ensureStarted, openPanel } =
  useTasks()

ensureStarted()

function typeLabel(type: TaskType | ''): string {
  if (type === 'create_dataset') return '出题'
  if (type === 'evaluate_dataset') return '评测'
  if (type === 'collect_database') return '采集'
  return '任务'
}

function statusLabel(status: TaskStatus): string {
  if (status === 'success') return '已完成'
  if (status === 'error') return '失败'
  return '进行中'
}

function statusTag(status: TaskStatus): string {
  if (status === 'success') return 'tag--mint'
  if (status === 'error') return 'tag--danger'
  return 'tag--sky'
}

/** 摘要行：显示最后一帧；还没有帧说明刚投递 */
function message(task: TaskSnapshot): string {
  const { steps } = task.meta
  return steps.length ? (steps[steps.length - 1]?.message ?? '') : '已投递，等待进度…'
}

/** 投递时的入参摘要 */
function summary(task: TaskSnapshot): string {
  const { inputs, type } = task.meta

  if (type === 'create_dataset') {
    const size = inputs.size === null ? '' : `${inputs.size} 个问题`
    const tables = inputs.table_ids.length ? `${inputs.table_ids.length} 张表` : ''
    return [size, tables].filter(Boolean).join(' · ')
  }

  const database = inputs.database_id ? `业务库 ${inputs.database_id}` : ''
  return [type === 'collect_database' ? database : '', inputs.dataset_id ? `数据集 ${inputs.dataset_id}` : '']
    .filter(Boolean)
    .join(' · ')
}

/**
 * 任务落到页面的去处：数据集任务去数据集页（评测直接落在「评测结果」页签），
 * 采集任务去表与字段页。
 */
function target(task: TaskSnapshot): { name: string; query: Record<string, string> } | null {
  const { inputs, outputs, type } = task.meta
  const dataset_id = outputs.dataset_id ?? inputs.dataset_id

  if (dataset_id && type !== 'collect_database') {
    return {
      name: 'dataset',
      query: { dataset_id, tab: type === 'evaluate_dataset' ? 'results' : 'items' },
    }
  }

  const database_id = outputs.database_id ?? inputs.database_id
  if (database_id) return { name: 'table', query: { database_id } }

  return null
}

function targetLabel(task: TaskSnapshot): string {
  if (task.meta.type === 'evaluate_dataset') return '查看这一轮结果'
  if (task.meta.type === 'collect_database') return '查看表与字段'
  return '查看数据集'
}

function meanLabel(mean: number | null): string {
  if (mean === null || Number.isNaN(mean)) return '未评分'
  return `${(mean * 100).toFixed(1)}%`
}

async function go(task: TaskSnapshot): Promise<void> {
  const to = target(task)
  if (!to) return

  panelOpen.value = false
  await router.push({ name: to.name, query: to.query })
}
</script>

<template>
  <el-button @click="openPanel">
    <AppIcon name="clock" :size="16" />
    <span>任务</span>
    <span v-if="runningCount" class="tag tag--sky">{{ runningCount }}</span>
  </el-button>

  <el-drawer v-model="panelOpen" title="任务" size="520px">
    <div class="task-panel">
      <div class="task-panel__head">
        <span class="muted-12">
          当前用户最近的一批任务（新的在前，最多 20 条、记录最多留 3 天）。
          <template v-if="runningCount">进行中 {{ runningCount }} 个。</template>
        </span>
        <el-button link type="primary" :loading="loading" @click="openPanel">刷新</el-button>
      </div>

      <p v-if="error" class="alert alert--danger">{{ error }}</p>

      <el-empty
        v-if="!tasks.length && !loading"
        class="empty-cell"
        description="还没有任务，投递一次出题、评测或采集就会出现在这里"
        :image-size="96"
      />

      <el-collapse v-else v-model="openNames">
        <el-collapse-item v-for="task in tasks" :key="task.id" :name="task.id">
          <template #title>
            <div class="row-head">
              <span class="tag" :class="statusTag(task.status)">{{ statusLabel(task.status) }}</span>
              <span class="cell-name">{{ typeLabel(task.meta.type) }}</span>
              <span class="muted-12 row-head__message" :title="message(task)">
                {{ message(task) }}
              </span>
            </div>
          </template>

          <p v-if="summary(task)" class="muted-12 task-panel__line">{{ summary(task) }}</p>
          <p class="mono task-panel__line" :title="task.id">任务 {{ task.id }}</p>

          <p v-if="task.error" class="alert alert--danger">{{ task.error }}</p>

          <div class="task__steps">
            <StepTimeline :steps="task.meta.steps" :running="task.status === 'running'" />
          </div>

          <p
            v-if="task.status === 'success' && task.meta.outputs.result_id"
            class="muted-12 task-panel__line"
          >
            SQL 等效率 {{ meanLabel(task.meta.outputs.mean) }} · 结果
            {{ task.meta.outputs.result_id }}
          </p>

          <el-button v-if="target(task)" link type="primary" @click="go(task)">
            <AppIcon name="arrow-right" :size="14" />
            <span>{{ targetLabel(task) }}</span>
          </el-button>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-drawer>
</template>

<style scoped>
.task-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.task-panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.task-panel__line {
  margin: 0 0 6px;
  overflow-wrap: anywhere;
}

.task__steps {
  max-height: 280px;
  overflow-y: auto;
  padding-right: 4px;
  margin-bottom: 12px;
}

.row-head {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  overflow: hidden;
}

.row-head__message {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
</style>
