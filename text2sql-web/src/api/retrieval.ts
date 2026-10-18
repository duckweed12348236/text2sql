import { buildPath, stream } from '@/plugins/request'
import type { ContextTable } from '@/types/dataset'
import type { RetrievalQuestion, RetrievalStatus, RetrievalStep } from '@/types/retrieval'

/** 检索问答：提问 → SQL → 执行，整条链路的进度以 SSE 逐帧回来 */

function toContexts(raw: unknown): ContextTable[] | null {
  return Array.isArray(raw) ? (raw as ContextTable[]) : null
}

function toStep(raw: unknown): RetrievalStep {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const status: RetrievalStatus =
    record.status === 'success' || record.status === 'error' ? record.status : 'running'

  return {
    message: typeof record.message === 'string' ? record.message : '',
    status,
    error: typeof record.error === 'string' ? record.error : null,
    sql: typeof record.sql === 'string' ? record.sql : null,
    data: record.data ?? null,
    table_infos: toContexts(record.table_infos),
  }
}

/**
 * 提问：跑一轮检索，每收到一帧就回调一次。
 * 流正常关闭、或 `signal` 中断即返回；整轮失败不以 HTTP 错误码体现，
 * 而是以一帧 `status` 为 `error` 的上报收尾（参数校验失败才是 `422` JSON 错误体）。
 */
export function askQuestion(
  payload: RetrievalQuestion,
  onStep: (step: RetrievalStep) => void,
  signal?: AbortSignal,
): Promise<void> {
  return stream(
    buildPath('retrieval'),
    payload,
    data => {
      try {
        onStep(toStep(JSON.parse(data)))
      } catch {
        // 单帧不是合法 JSON 就跳过，不打断整条流
      }
    },
    { rawIntegerFields: ['database_id'], signal },
  )
}
