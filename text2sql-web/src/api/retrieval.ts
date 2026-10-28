import { buildPath, del, get, parseJSONPreservingIds, stream } from '@/plugins/request'
import { toItem, toList } from '@/api/shape'
import type { ContextTable } from '@/types/dataset'
import type {
  RetrievalQuestion,
  RetrievalSession,
  RetrievalStatus,
  RetrievalStep,
  RetrievalToken,
  SessionDetail,
  SessionTurn,
} from '@/types/retrieval'

/** 检索问答：提问 → SQL → 执行，整条链路的进度以 SSE 逐帧回来 */

function toContexts(raw: unknown): ContextTable[] | null {
  return Array.isArray(raw) ? (raw as ContextTable[]) : null
}

function toNumber(raw: unknown): number {
  if (typeof raw === 'number') return raw
  if (typeof raw === 'string' && raw.trim()) return Number(raw)
  return 0
}

/** 累计 Token 用量；缺字段或不是对象时按 null（该帧没带） */
function toToken(raw: unknown): RetrievalToken | null {
  if (raw === null || typeof raw !== 'object') return null
  const record = raw as Record<string, unknown>
  return {
    prompt: toNumber(record.prompt),
    completion: toNumber(record.completion),
    total: toNumber(record.total),
  }
}

/** SSE 帧只做宽松归一：缺字段按 null / running 处理 */
function toStep(raw: unknown): RetrievalStep {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const status: RetrievalStatus =
    record.status === 'success' || record.status === 'error' ? record.status : 'running'

  return {
    message: typeof record.message === 'string' ? record.message : '',
    status,
    error: typeof record.error === 'string' ? record.error : null,
    clarification: typeof record.clarification === 'string' ? record.clarification : null,
    thread_id:
      record.thread_id === undefined || record.thread_id === null ? null : String(record.thread_id),
    session_id:
      record.session_id === undefined || record.session_id === null
        ? null
        : String(record.session_id),
    sql: typeof record.sql === 'string' ? record.sql : null,
    data: record.data ?? null,
    table_infos: toContexts(record.table_infos),
    token: toToken(record.token),
  }
}

export interface AskOptions {
  /** 中断这条流 */
  signal?: AbortSignal
  /** 新会话的编号随响应头 `X-Session-Id` 先回来，读第一帧之前就能拿到 */
  onSession?: (session_id: string) => void
}

/**
 * 提问：跑一轮检索，每收到一帧就回调一次。
 * 流正常关闭、或 `signal` 中断即返回；整轮失败不以 HTTP 错误码体现，
 * 而是以一帧 `status` 为 `error` 的上报收尾（参数校验失败才是 `422` JSON 错误体，
 * `session_id` 查不到时在开流之前返回 `404`）。
 */
export function askQuestion(
  payload: RetrievalQuestion,
  onStep: (step: RetrievalStep) => void,
  options: AskOptions = {},
): Promise<void> {
  return stream(
    buildPath('retrieval'),
    payload,
    data => {
      try {
        // 帧里的 session_id 是雪花 ID，必须按字符串保留，否则回传的是被取整的编号
        onStep(toStep(parseJSONPreservingIds(data)))
      } catch {
        // 单帧不是合法 JSON 就跳过，不打断整条流
      }
    },
    {
      rawIntegerFields: ['database_id', 'session_id'],
      signal: options.signal,
      onResponse: response => {
        const id = response.headers.get('X-Session-Id')
        if (id) options.onSession?.(id)
      },
    },
  )
}

/** 列出会话：按编号倒序，新的在前；轮次明细不在列表里 */
export async function listSessions(limit = 100): Promise<RetrievalSession[]> {
  const rows = toList<Record<string, unknown>>(
    await get<unknown>(buildPath('retrieval', 'session'), { limit }),
  )

  return rows.map(row => ({
    id: String(row.id ?? ''),
    thread_id: row.thread_id === undefined || row.thread_id === null ? null : String(row.thread_id),
    created_at: typeof row.created_at === 'string' ? row.created_at : '',
    turns: typeof row.turns === 'number' ? row.turns : 0,
    last_turn_at: typeof row.last_turn_at === 'string' ? row.last_turn_at : null,
  }))
}

function toSessionTurn(raw: unknown): SessionTurn {
  const record = raw !== null && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}

  return {
    id: record.id === undefined || record.id === null ? '' : String(record.id),
    round: toNumber(record.round),
    created_at: typeof record.created_at === 'string' ? record.created_at : '',
    input: typeof record.input === 'string' ? record.input : '',
    tokens: toNumber(record.tokens),
    steps: Array.isArray(record.steps) ? record.steps.map(toStep) : [],
  }
}

/**
 * 会话详情：会话本身 + 名下全部轮次（按 `round` 升序）。
 * 进度帧是整轮结束时整体写回的，正在跑的那一轮可能只到一半、甚至还是空数组。
 */
export async function getSessionDetail(session_id: string): Promise<SessionDetail> {
  const record = toItem<Record<string, unknown>>(
    await get<unknown>(buildPath('retrieval', 'session', session_id)),
  )

  return {
    id: record.id === undefined || record.id === null ? '' : String(record.id),
    thread_id:
      record.thread_id === undefined || record.thread_id === null
        ? null
        : String(record.thread_id),
    created_at: typeof record.created_at === 'string' ? record.created_at : '',
    tokens: toNumber(record.tokens),
    turns: Array.isArray(record.turns) ? record.turns.map(toSessionTurn) : [],
  }
}

/** 删除会话：先清检索记忆，再删登记；记忆清不干净时返回 503、登记原样保留 */
export function deleteSession(session_id: string): Promise<void> {
  return del<void>(buildPath('retrieval', 'session', session_id))
}
