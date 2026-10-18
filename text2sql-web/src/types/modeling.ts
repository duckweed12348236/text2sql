/**
 * 元数据管理接口的数据结构（见 api_docs.md）。
 *
 * 雪花 ID 是服务端生成的有符号 64 位整数，超出 JS 安全整数范围，
 * 请求层已把它们转成字符串，因此这里统一按 string 声明。
 */

/** 表类型 */
export type TableRole = 'fact' | 'dim'

/** 字段角色 */
export type ColumnRole = 'primary_key' | 'foreign_key' | 'measure' | 'dimension'

/** 数据库类型（当前只有 mysql） */
export type DatabaseType = 'mysql'

/** 业务库快照；连接地址含账号口令，只写不读、不在响应里 */
export interface Database {
  /** 雪花 ID */
  id: string
  name: string
  type: DatabaseType | null
  /** 附加配置（JSON） */
  extra: Record<string, unknown> | null
  /** 整库采集成功后会置位，登记就此锁死、不能再改 */
  collected: boolean
  /** 是否为默认库 */
  default: boolean
}

/** 新增业务库 */
export interface DatabaseCreate {
  name: string
  /** 连接地址（含账号口令），只写不读 */
  url: string
  type?: DatabaseType
  extra?: Record<string, unknown> | null
}

/** 修改业务库：只提交字段表示保留原值，提交 null 表示清空 */
export interface DatabaseUpdate {
  name?: string | null
  url?: string | null
  type?: DatabaseType | null
  extra?: Record<string, unknown> | null
}

export interface Table {
  /** 形如 `<database_id>.<表名>` */
  id: string
  name: string
  role: TableRole | null
  description: string | null
  /** 所属业务库的雪花 ID */
  database_id: string
}

export interface Column {
  /** 形如 `<table_id>.<字段名>` */
  id: string
  name: string
  /** 业务库的真实类型，来自采集登记，不提供修改 */
  type: string | null
  role: ColumnRole | null
  description: string | null
  alias: string[] | null
  /** 所属表编号 */
  table_id: string
  /** 是否采集取值，来自采集登记，不提供修改 */
  synchronized: boolean
}

export interface Metric {
  /** 雪花 ID */
  id: string
  name: string
  description: string | null
  alias: string[] | null
}

/** 修改表：只提交字段表示保留原值，提交 null 表示清空 */
export interface TableUpdate {
  role?: TableRole | null
  description?: string | null
}

/**
 * 修改字段：可改的只有角色、描述与别名。
 * `type` 与 `synchronized` 是采集口径的登记，接口不再接受（提交也会被当成额外字段忽略）。
 */
export interface ColumnUpdate {
  role?: ColumnRole | null
  description?: string | null
  alias?: string[] | null
}

/** 新增指标 */
export interface MetricCreate {
  name: string
  description?: string | null
  alias?: string[] | null
}

/** 修改指标 */
export interface MetricUpdate {
  name?: string | null
  description?: string | null
  alias?: string[] | null
}
