import type { ContextTable } from '@/types/dataset'

/**
 * 检索问答接口的数据结构（见 api_docs.md「检索问答」）。
 *
 * 与出题 / 评测不同，这条链路不落任务记录：`POST /retrieval` 挂着一条 SSE 流，
 * 每帧都是一次链路上报，跑完即结束。
 */

/** 帧状态：注意失败叫 `error`，与评测图进度帧的 `failed` 不是同一套取值 */
export type RetrievalStatus = 'running' | 'success' | 'error'

/** 一条链路上报（SSE 一帧） */
export interface RetrievalStep {
  message: string
  status: RetrievalStatus
  error: string | null
  /** 该节点产出的 SQL；执行完成帧上才是最终那条 */
  sql: string | null
  /** 查询结果，只有执行成功那一帧携带 */
  data: unknown
  /** 本次检索到的表 / 字段 / 取值（重排后的最终片段），与结果帧一起交出 */
  table_infos: ContextTable[] | null
}

/** 提问入参 */
export interface RetrievalQuestion {
  /** 待查询的自然语言问题 */
  content: string
  /** 本次检索的业务库编号（雪花 ID），不跨库 */
  database_id: string
}
