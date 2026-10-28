import type { ContextTable } from '@/types/dataset'

/**
 * 检索问答接口的数据结构（见 api_docs.md「检索问答」）。
 *
 * 与出题 / 评测不同，这条链路不落任务记录：`POST /retrieval` 挂着一条 SSE 流，
 * 每帧都是一次链路上报，跑完即结束；会话与轮次会落库，供会话列表与删除使用。
 */

/** 帧状态：注意失败叫 `error`，与评测图进度帧的 `failed` 不是同一套取值 */
export type RetrievalStatus = 'running' | 'success' | 'error'

/** 本轮到该帧为止的累计 Token 用量（只统计链路里的大模型调用） */
export interface RetrievalToken {
  prompt: number
  completion: number
  total: number
}

/** 一条链路上报（SSE 一帧） */
export interface RetrievalStep {
  message: string
  status: RetrievalStatus
  error: string | null
  /** 反问用户的一句话（问题模糊、或指标口径对不上时携带，此时 `error` 为空） */
  clarification: string | null
  /** 检索图的会话编号（雪花 ID 的十进制文本），客户端不必用它 */
  thread_id: string | null
  /** 本系统的会话编号，下一轮把它回传即接回同一会话 */
  session_id: string | null
  /** 该节点产出的 SQL；执行完成帧上才是最终那条 */
  sql: string | null
  /** 查询结果，只有执行成功那一帧携带 */
  data: unknown
  /** 本次检索到的表 / 字段 / 取值（重排后的最终片段），与结果帧一起交出 */
  table_infos: ContextTable[] | null
  /** 累计 Token 用量，每帧都带；最后一帧的 `total` 即本轮总量 */
  token: RetrievalToken | null
}

/** 提问入参 */
export interface RetrievalQuestion {
  /** 待查询的自然语言问题；反问后继续时是用户对反问的回答 */
  content: string
  /** 本次检索的业务库编号（雪花 ID），不跨库 */
  database_id: string
  /**
   * 会话编号：不带即开一轮新对话；带上被中断那一轮的编号时，
   * `content` 视为对该反问的回答，图从停住的地方继续。
   */
  session_id?: string | null
}

/**
 * 一轮问答的渲染模型：历史轮次由会话详情（`GET /retrieval/session/{id}`）映射而来，
 * 正在跑的那一轮由 SSE 帧在本地拼出来，两者同构、共用同一个轮次卡片。
 */
export interface TurnRecord {
  /** 本地轮次编号（服务端不给轮次编号） */
  id: string
  /** 所属会话编号；提交失败、还没拿到会话时可能为空 */
  session_id: string
  /** 用户输入：问题本身，或对上一轮反问的回答 */
  content: string
  /** 本轮全部进度帧（存档里已去掉帧上的 data / table_infos，避免重复占空间） */
  steps: RetrievalStep[]
  /** 最终 SQL */
  sql: string
  /** 查询结果原样 */
  data: unknown
  /** 本轮检索到的表 / 字段 / 取值 */
  contexts: ContextTable[]
  /** 整轮失败原因 */
  failure: string
  /** 反问语句：有值说明这一轮停在这里等补充 */
  clarification: string
  /** 累计 Token 用量 */
  token: RetrievalToken | null
  /** 开始时间（ISO） */
  started_at: string
}

/** 会话详情里的一轮（进度帧在整轮结束时整体写回，跑着的那一轮可能只到一半） */
export interface SessionTurn {
  /** 轮次编号（雪花 ID） */
  id: string
  /** 轮次序号，同一会话内从 1 开始 */
  round: number
  created_at: string
  /** 用户这一轮的原始输入（不是改写后的问题） */
  input: string
  /** 这一轮的 Token 用量 */
  tokens: number
  /** 这一轮的进度帧，结构与 SSE 每帧相同，可能为空数组 */
  steps: RetrievalStep[]
}

/** GET /retrieval/session/{session_id} 的出参 */
export interface SessionDetail {
  id: string
  thread_id: string | null
  created_at: string
  /** 会话累计 Token 用量（各轮之和） */
  tokens: number
  turns: SessionTurn[]
}

/** 会话列表里的一条（GET /retrieval/session，不含轮次明细） */
export interface RetrievalSession {
  /** 本系统的会话编号（雪花 ID） */
  id: string
  /** 检索图的会话编号，服务端内部使用 */
  thread_id: string | null
  created_at: string
  /** 这条会话下的轮次数 */
  turns: number
  /** 最近一轮的创建时间，还没有轮次时为 null */
  last_turn_at: string | null
}
