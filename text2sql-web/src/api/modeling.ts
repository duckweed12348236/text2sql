import { buildPath, del, get, post, put } from '@/plugins/request'
import { toItem, toList } from '@/api/shape'
import type {
  Column,
  ColumnUpdate,
  Database,
  DatabaseCreate,
  DatabaseUpdate,
  Metric,
  MetricCreate,
  MetricUpdate,
  Table,
  TableUpdate,
} from '@/types/modeling'

/** 列出已登记的业务库，新登记的在前 */
export async function listDatabases(): Promise<Database[]> {
  return toList<Database>(await get<unknown>(buildPath('modeling', 'database')))
}

/** 新增业务库；编号由服务端生成，`collected` 从「未采集」开始 */
export async function createDatabase(payload: DatabaseCreate): Promise<Database> {
  return toItem<Database>(await post<unknown>(buildPath('modeling', 'database'), payload))
}

/** 修改业务库；已采集的库登记锁死，会拿到 409 */
export async function updateDatabase(
  database_id: string,
  payload: DatabaseUpdate,
): Promise<Database> {
  return toItem<Database>(
    await put<unknown>(buildPath('modeling', 'database', database_id), payload),
  )
}

/** 删除业务库：连同表、字段与向量集合一起删 */
export function deleteDatabase(database_id: string): Promise<void> {
  return del<void>(buildPath('modeling', 'database', database_id))
}

/** 发起整库采集（后台任务），返回任务编号；进度轮询 `GET /evaluation/task` */
export async function collectDatabase(database_id: string): Promise<string> {
  const record = toItem<Record<string, unknown>>(
    await post<unknown>(buildPath('modeling', 'database', database_id, 'collect')),
  )

  return record.task_id === undefined || record.task_id === null ? '' : String(record.task_id)
}

/** 列出某业务库下的表，`name` 为表名关键字、子串匹配；按表名排序 */
export async function listTables(database_id: string, name?: string): Promise<Table[]> {
  return toList<Table>(await get<unknown>(buildPath('modeling', 'table'), { database_id, name }))
}

/** 修改表 */
export async function updateTable(table_id: string, payload: TableUpdate): Promise<Table> {
  return toItem<Table>(await put<unknown>(buildPath('modeling', 'table', table_id), payload))
}

/** 列出某张表下的字段 */
export async function listColumns(table_id: string): Promise<Column[]> {
  return toList<Column>(await get<unknown>(buildPath('modeling', 'column'), { table_id }))
}

/** 修改字段 */
export async function updateColumn(column_id: string, payload: ColumnUpdate): Promise<Column> {
  return toItem<Column>(await put<unknown>(buildPath('modeling', 'column', column_id), payload))
}

/** 列出指标 */
export async function listMetrics(): Promise<Metric[]> {
  return toList<Metric>(await get<unknown>(buildPath('modeling', 'metric')))
}

/** 新增指标 */
export async function createMetric(payload: MetricCreate): Promise<Metric> {
  return toItem<Metric>(await post<unknown>(buildPath('modeling', 'metric'), payload))
}

/** 修改指标 */
export async function updateMetric(metric_id: string, payload: MetricUpdate): Promise<Metric> {
  return toItem<Metric>(await put<unknown>(buildPath('modeling', 'metric', metric_id), payload))
}

/** 删除指标，返回 204 无响应体 */
export function deleteMetric(metric_id: string): Promise<void> {
  return del<void>(buildPath('modeling', 'metric', metric_id))
}
