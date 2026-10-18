# 接口文档

后端为 FastAPI 应用，各控制器的路由在 `app.py` 中直接 `include_router`，没有全局前缀，下列 URL 均为完整路径。

响应体由 `Model.dump()`（`models/__init__.py`）生成，字段与模型定义一致（元数据见 `models/meta.py`，评测见
`models/dataset.py`）；响应中不含外键关系对象，只含外键字段（如 `database_id`、`table_id`、`dataset_id`）。
后台任务快照（`/evaluation/task`）是例外：它来自 Redis 里的任务记录（`plugins.redis.Task.dump`），结构见
[接口 3](#3-轮询任务进度)。

出题、评测与整库采集都是后台任务：投递接口只返回任务编号与 `202`，进度靠轮询任务接口取——轮询入口只有一个，
即 `/evaluation/task`（见[评测数据集接口 3](#3-轮询任务进度)），采集任务也用它。检索问答（`/retrieval`）则是
SSE 流式返回，不落任务记录。

## 模块一览

| 前缀          | 来源                        | 说明                                       |
|---------------|-----------------------------|--------------------------------------------|
| `/modeling`   | `controllers/modeling.py`   | 元数据管理（库、表、字段、指标）           |
| `/evaluation` | `controllers/evaluation.py` | 评测数据集（出题与评测，后台任务 + 轮询）  |
| `/retrieval`  | `controllers/retrieval.py`  | 检索问答（自然语言问题 → SQL 并执行，SSE） |

## 元数据管理（`/modeling`）

### 枚举取值

| 枚举                               | 取值                                                       |
|------------------------------------|------------------------------------------------------------|
| `DatabaseTypeOption`（数据库类型） | `mysql`                                                    |
| `TableRoleOption`（表类型）        | `fact` \| `dim`                                            |
| `ColumnRoleOption`（字段角色）     | `primary_key` \| `foreign_key` \| `measure` \| `dimension` |

### 接口一览

| 方法   | URL                                        | 说明                                 |
|--------|--------------------------------------------|--------------------------------------|
| GET    | `/modeling/database`                       | 列出已登记的业务库                   |
| POST   | `/modeling/database`                       | 新增业务库                           |
| PUT    | `/modeling/database/{database_id}`         | 修改业务库（已采集的库不能改）       |
| DELETE | `/modeling/database/{database_id}`         | 删除业务库（连同表、字段与向量集合） |
| POST   | `/modeling/database/{database_id}/collect` | 发起整库采集（后台任务）             |
| GET    | `/modeling/table`                          | 列出某业务库下的表                   |
| PUT    | `/modeling/table/{table_id}`               | 修改表                               |
| GET    | `/modeling/column`                         | 列出某张表下的字段                   |
| PUT    | `/modeling/column/{column_id}`             | 修改字段                             |
| GET    | `/modeling/metric`                         | 列出指标                             |
| POST   | `/modeling/metric`                         | 新增指标                             |
| PUT    | `/modeling/metric/{metric_id}`             | 修改指标                             |
| DELETE | `/modeling/metric/{metric_id}`             | 删除指标                             |

### 通用约定

- 修改类接口（PUT）按 `exclude_unset` 区分「未提交」与「提交 `null`」：未提交的字段保持原值，显式提交 `null` 表示清空该字段；一个字段都没提交时返回
  `422`，`detail` 为 `没有可修改的字段`。
- 请求体中未定义的额外字段会被忽略（请求体 Schema 未开启 `extra="forbid"`）。
- 错误响应体为 FastAPI 默认格式：`{"detail": "错误信息"}`；参数校验失败为 `422`，`detail` 为数组。
- 表编号 `table_id` 形如 `<Database.id>.<表名>`，字段编号 `column_id` 形如 `<表编号>.<字段名>`；编号由数据库编号 /
  表编号与名字拼出，故名字与归属都不提供修改。
- 库编号 `Database.id`、指标编号 `Metric.id` 为雪花 ID（服务端生成的有符号 64 位整数），在 JSON 中可能超出 JS 安全整数范围。
- 业务库的登记在整库采集成功后 **锁死**：`PUT /modeling/database/{database_id}` 对已采集的库（`collected` 为 `true`）
  一律返回 `409 该数据库已采集，不能再修改`，改名也不行——要改就先删掉登记、重新登记再采集。
- 整库采集是后台任务：`POST /modeling/database/{database_id}/collect` 只返回任务编号与 `202`，进度轮询
  `/evaluation/task`（见[评测数据集接口 3](#3-轮询任务进度)）。
- 字段改动会同步向量库，向量库同步失败时为 `500`（元数据库的改动已生效，重跑一轮采集即可补齐）；其余错误为
  `404`（资源不存在）、`409`（已采集的库不能改）、`422`（参数校验失败或没有可修改的字段）。

### 接口明细

#### 1. 列出业务库

- **方法/URL**：`GET /modeling/database`
- **入参**：无
- **出参**：`200`，数组，元素为业务库快照，按编号倒序（新登记的在前）
- **说明**：连接地址不返回（其中含账号口令），因此底层用 `only(...)` 查询、响应里也没有这一列；
  `collected` 为 `true` 的库已整库采集过、登记已锁死不能再改（见接口 3），`type` / `collected` 据此展示与禁用编辑。

```json
[
  {
    "id": 1234567890123456789,
    "name": "业务库名",
    "type": "mysql",
    "extra": null,
    "collected": true,
    "default": true
  }
]
```

#### 2. 新增业务库

- **方法/URL**：`POST /modeling/database`
- **入参 Body**（`DatabaseCreate`）：

| 字段    | 类型                 | 必填 | 约束         | 说明                                           |
|---------|----------------------|------|--------------|------------------------------------------------|
| `name`  | string               | 是   | 1–128        | 业务库名称                                     |
| `url`   | string               | 是   | 1–255        | 连接地址（含账号口令，只写不读）               |
| `type`  | `DatabaseTypeOption` | 否   | 默认 `mysql` | 数据库类型                                     |
| `extra` | object \| `null`     | 否   |              | 附加配置（JSON，整组替换，可提交 `null` 清空） |

```json
{
  "name": "业务库名",
  "url": "mysql://user:password@host:3306/dbname",
  "type": "mysql",
  "extra": null
}
```

- **出参**：`200`，业务库快照（同接口 1，不含 `url`）
- **错误**：`422` 参数校验失败
- **说明**：编号是雪花 ID、由服务端生成；`collected` 从「未采集」开始——整库采集成功后才由采集任务置位
  （见接口 5），故请求体里没有这个字段。登记完顺手把连接注册进 Tortoise（见 `models.register_connection`），
  本进程里立刻能按编号取到该库的连接（检索、采集都用它），不必等重启。

#### 3. 修改业务库

- **方法/URL**：`PUT /modeling/database/{database_id}`
- **入参**：

Path：

| 名称          | 类型 | 必填 | 说明                  |
|---------------|------|------|-----------------------|
| `database_id` | int  | 是   | 业务库编号（雪花 ID） |

Body（`DatabaseUpdate`，至少提交一个字段）：

| 字段    | 类型                           | 必填 | 说明                                    |
|---------|--------------------------------|------|-----------------------------------------|
| `name`  | string \| `null`               | 否   | 1–128                                   |
| `url`   | string \| `null`               | 否   | 1–255，连接地址（含账号口令，只写不读） |
| `type`  | `DatabaseTypeOption` \| `null` | 否   | 数据库类型                              |
| `extra` | object \| `null`               | 否   | 附加配置（JSON，整组替换）              |

- **出参**：`200`，业务库快照（同接口 1，不含 `url`）
- **错误**：`404` `指定的数据库不存在`；`422` `没有可修改的字段`；`409` `该数据库已采集，不能再修改`
- **说明**： **已采集的库不能再改**（`collected` 为 `true` 时一律 `409`，改名也不行）——采集落下的表编号、
  字段、取值与向量都挂在这条登记（尤其是连接地址）上，改了登记等于换一个库；要改就先删掉这条登记、
  重新登记再采集。连接地址变了会同步连接（见 `models.register_connection`），类型不影响连接、不必重连。

#### 4. 删除业务库

- **方法/URL**：`DELETE /modeling/database/{database_id}`
- **入参**：

Path：

| 名称          | 类型 | 必填 | 说明                  |
|---------------|------|------|-----------------------|
| `database_id` | int  | 是   | 业务库编号（雪花 ID） |

- **出参**：`204`
- **错误**：`404` `指定的数据库不存在`
- **说明**：连同它名下已登记的表与字段一起删（外键是 RESTRICT，先删字段再删表再删库，三步一个事务），
  向量库里该库的字段集合与取值集合也一并清掉（`Qdrant.delete_database`，Qdrant 不可用时只记日志、
  不影响删除），连接一并摘掉（`models.discard_connection`）。已采集与否不影响删除。

#### 5. 采集业务库

- **方法/URL**：`POST /modeling/database/{database_id}/collect`
- **入参**：

Path：

| 名称          | 类型 | 必填 | 说明                  |
|---------------|------|------|-----------------------|
| `database_id` | int  | 是   | 业务库编号（雪花 ID） |

- **出参**：`202`，`application/json`——采集任务已投进队列，采集还没开始：

```json
{
  "task_id": 1234567890123456793
}
```

| 字段      | 类型 | 说明                                                            |
|-----------|------|-----------------------------------------------------------------|
| `task_id` | int  | 任务编号（雪花 ID）：据此轮询任务进度（见「评测数据集」接口 3） |

- **进度**：不在本接口返回，轮询 `GET /evaluation/task`（见「评测数据集」接口 3）取；采集帧的字段只有
  `message` / `status` / `error`（`plugins.agents.modeling.schemas.Step`， **没有** `dataset_id`）。典型一轮：

  | 帧 `message`                       | `status`  |
      |------------------------------------|-----------|
  | 开始采集表结构：待采集 N 张表      | `running` |
  | 已采集 k/N 张表（逐批上报）        | `running` |
  | 表采集完成：N/M 张，X 张跳过       | `success` |
  | 开始向量化字段：待向量化 N 个字段  | `running` |
  | 已向量化 k/N 个字段（逐批上报）    | `running` |
  | 字段向量化完成：N 个，X 个失败     | `success` |
  | 开始采集取值：待采集 N 个字段的取值 | `running` |
  | 已采集 k/N 个字段的取值（逐批上报） | `running` |
  | 取值采集完成：N 个字段，X 个字段失败 | `success` |

  单表 / 单字段失败只推一条 `status` 为 `failed` 的帧（如「表 order 采集失败」），流程继续走；整体中断时
  以「采集中断：N 张表未采集」之类的失败帧收尾，任务的 `status` 随之为 `error`。某一阶段没有可处理的对象时
  （库里没表、没有待向量化的字段等）会推一条 `success` 帧（如「没有待采集的表」）直接跳到下一阶段。
- **说明**：采集整库进行（登记表结构 → 分析字段 → 向量化 → 采集取值），一轮以分钟计，故与出题、评测一样
  走后台任务：任务类型为 `collect_database`，进度帧与产出都在任务记录里，采集成功后任务的
  `meta.outputs` 会带上 `database_id` 与 `collected: true`，同时业务库列表里的 `collected` 置位
  （登记就此锁死，见接口 3）。已采集的库可以再采（业务库结构变了、或上次采集有失败帧，重跑一轮覆盖即可）。
- **依赖**：任务由应用启动时拉起的 Celery worker 执行；worker 没起来时任务只排队、不执行，轮询会一直停在
  `running`。采集要连上业务库本身（用登记的连接地址），连不上会以失败帧结束（轮询里看得到原因）。
- **错误**：`404` `指定的数据库不存在`；`422` 参数校验失败

#### 6. 列出表

- **方法/URL**：`GET /modeling/table`
- **入参（Query）**：

| 名称          | 类型   | 必填 | 约束     | 说明                                                                            |
|---------------|--------|------|----------|---------------------------------------------------------------------------------|
| `database_id` | int    | 是   | `ge=1`   | 业务库编号                                                                      |
| `name`        | string | 否   | 最长 128 | 表名关键字，不区分大小写的子串匹配（`LIKE '%name%'`）；不传或只传空白视作不过滤 |

- **出参**：`200`，数组，元素为 `Table`，按表名排序
- **错误**：`404` `指定的数据库不存在`（库未登记；库下没有表、或没有匹配的表时返回 `200` 与空数组）

```json
[
  {
    "id": "1234567890123456789.order",
    "name": "order",
    "role": "fact",
    "description": "订单表",
    "database_id": 1234567890123456789
  }
]
```

- **说明**：关键字里的 `%` 与 `_` 会被当作 LIKE 的通配符参与匹配，本接口只用于选表，不能拿它做唯一性判断。

#### 7. 修改表

- **方法/URL**：`PUT /modeling/table/{table_id}`
- **入参**：

Path：

| 名称       | 类型   | 必填 | 说明                                |
|------------|--------|------|-------------------------------------|
| `table_id` | string | 是   | 表编号，形如 `<database_id>.<表名>` |

Body（`TableUpdate`，至少提交一个字段）：

| 字段          | 类型                      | 必填 | 说明                     |
|---------------|---------------------------|------|--------------------------|
| `role`        | `fact` \| `dim` \| `null` | 否   | 表类型，提交 `null` 清空 |
| `description` | string \| `null`          | 否   | 表描述，提交 `null` 清空 |

```json
{
  "role": "dim",
  "description": "维度表"
}
```

- **出参**：`200`，单个 `Table`（结构同接口 6）
- **错误**：`404` `指定的表不存在`；`422` `没有可修改的字段`
- **说明**：`name` 与 `database_id` 不可改——两者一起决定表编号，表编号又被字段编号引用，改名等于换一张表。
  本接口 **不涉及向量库**：向量库里只有字段（`columns.<库编号>`）与取值（`values.<库编号>`）两类点，表本身不参与
  向量化；检索用的表信息是按字段载荷里的 `table_id` 回元数据库取的，改完即生效。

#### 8. 列出字段

- **方法/URL**：`GET /modeling/column`
- **入参（Query）**：

| 名称       | 类型   | 必填 | 约束           |
|------------|--------|------|----------------|
| `table_id` | string | 是   | `min_length=1` |

- **出参**：`200`，数组，元素为 `Column`
- **错误**：`404` `指定的表不存在`（表未登记；表下没有字段时返回 `200` 与空数组）

```json
[
  {
    "id": "1234567890123456789.order.status",
    "name": "status",
    "type": "varchar(32)",
    "role": "dimension",
    "description": "订单状态",
    "alias": [
      "状态"
    ],
    "table_id": "1234567890123456789.order",
    "synchronized": true
  }
]
```

#### 9. 修改字段

- **方法/URL**：`PUT /modeling/column/{column_id}`
- **入参**：

Path：

| 名称        | 类型   | 必填 | 说明                               |
|-------------|--------|------|------------------------------------|
| `column_id` | string | 是   | 字段编号，形如 `<表编号>.<字段名>` |

Body（`ColumnUpdate`，至少提交一个字段）：

| 字段          | 类型                         | 必填 | 约束/说明              |
|---------------|------------------------------|------|------------------------|
| `role`        | `ColumnRoleOption` \| `null` | 否   | 字段角色               |
| `description` | string \| `null`             | 否   | 字段描述               |
| `alias`       | string[] \| `null`           | 否   | 整组替换，不与原值合并 |

```json
{
  "role": "dimension",
  "description": "订单状态",
  "alias": [
    "状态"
  ]
}
```

- **出参**：`200`，单个 `Column`（结构同接口 8）
- **错误**：`404` `指定的字段不存在`；`422` `没有可修改的字段`；同步向量库失败（Qdrant 不可用、嵌入模型报错）时为 `500`，
  此时元数据库的改动 **已经生效**，重跑一轮采集即可补齐。
- **说明**：`name` 与 `table_id` 不可改（决定字段编号，且编号被向量库中的字段点引用）；`type` 与 `synchronized`
  也不在可改字段里——两者都是采集口径的登记（业务库的真实类型、取值要不要采集，见
  `plugins.agents.modeling.nodes.discover_tables` 与 `models.is_value_type`），要改就重跑一轮采集。
  这两个字段即使提交也按额外字段忽略（见「通用约定」），不报错；只提交它们时会因为「没有可修改的字段」拿到 `422`。
- **向量库同步**：改完元数据库后按改动同步向量库（见 `plugins.qdrant.Qdrant.update_column`）——`description` / `alias`
  参与向量化（与字段名各成一个点），改了就整批删旧点、重新向量化；只改 `role` 时只覆盖点的载荷，不必调用嵌入模型。

#### 10. 列出指标

- **方法/URL**：`GET /modeling/metric`
- **入参**：无
- **出参**：`200`，数组，元素为 `Metric`

```json
[
  {
    "id": 1234567890123456789,
    "name": "GMV",
    "description": "成交总额",
    "alias": [
      "销售额"
    ]
  }
]
```

#### 11. 新增指标

- **方法/URL**：`POST /modeling/metric`
- **入参 Body**（`MetricCreate`）：

| 字段          | 类型               | 必填 | 约束  |
|---------------|--------------------|------|-------|
| `name`        | string             | 是   | 1–128 |
| `description` | string \| `null`   | 否   |       |
| `alias`       | string[] \| `null` | 否   |       |

```json
{
  "name": "GMV",
  "description": "成交总额",
  "alias": [
    "销售额"
  ]
}
```

- **出参**：`200`，单个 `Metric`（含服务端生成的编号，结构同接口 10）
- **说明**：指标不归属具体业务库（跨库使用），故无 `database_id`；当前只登记元数据库——向量库 `metrics`
  集合尚无写入接口，新登记的指标暂不会被检索召回。

#### 12. 修改指标

- **方法/URL**：`PUT /modeling/metric/{metric_id}`
- **入参**：

Path：

| 名称        | 类型 | 必填 | 说明                |
|-------------|------|------|---------------------|
| `metric_id` | int  | 是   | 指标编号（雪花 ID） |

Body（`MetricUpdate`，至少提交一个字段）：

| 字段          | 类型               | 必填 | 约束/说明 |
|---------------|--------------------|------|-----------|
| `name`        | string \| `null`   | 否   | 1–128     |
| `description` | string \| `null`   | 否   |           |
| `alias`       | string[] \| `null` | 否   | 整组替换  |

```json
{
  "name": "GMV",
  "alias": [
    "销售额",
    "成交额"
  ]
}
```

- **出参**：`200`，单个 `Metric`
- **错误**：`404` `指定的指标不存在`；`422` `没有可修改的字段`
- **说明**：指标编号与名字无关，改名不牵动任何引用，故 `name` 允许修改。

#### 13. 删除指标

- **方法/URL**：`DELETE /modeling/metric/{metric_id}`
- **入参**：

| 名称        | 位置 | 类型 | 必填 | 说明                |
|-------------|------|------|------|---------------------|
| `metric_id` | Path | int  | 是   | 指标编号（雪花 ID） |

- **出参**：`204`，无响应体
- **错误**：`404` `指定的指标不存在`
- **说明**：只清元数据库——向量库 `metrics` 集合中的指标点不会随之清除，检索按向量库的点还原指标（见
  `plugins.agents.retrieval.nodes.recall_metrics`），故已删除的指标仍可能被召回；要清干净需自行按 payload 中的 `id` 从该集合删点。

## 评测数据集（`/evaluation`）

评估图分两步跑：一轮出题（生成问题与 SQL → 校验纠正 → 落库数据集）在评测前停住，再用同一个会话编号恢复执行一轮评测
（见 `plugins.agents.evaluation.graph.Evaluator.invoke`）。数据集编号、条目编号与评测结果编号都是雪花 ID。
出题所用的表由调用方指定（新建数据集接口的 `table_ids`），评估图不再自己挑表。

**两步都是后台任务**：投递（接口 1、2）各自拿到一个任务编号，进度写进 Redis 的任务记录，靠轮询（接口 3）看；
接口本身只投递、不等待。

> **每轮评测都会产出一份 Markdown 解读报告**（`Result.report`），随「查看数据集」接口按原样返回——详见下文
> [评测结果报告是 Markdown 正文](#评测结果报告是-markdown-正文resultreport)。

### 接口一览

| 方法   | URL                                | 说明                                           |
|--------|------------------------------------|------------------------------------------------|
| POST   | `/evaluation/dataset`              | 新建数据集（出题，后台任务，立即返回任务编号） |
| POST   | `/evaluation`                      | 评估数据集（后台任务，立即返回任务编号）       |
| GET    | `/evaluation/task`                 | 轮询任务进度（按用户批量查，一次看多个任务）   |
| GET    | `/evaluation/dataset`              | 列出数据集                                     |
| GET    | `/evaluation/dataset/{dataset_id}` | 查看数据集（条目与评测结果，含 Markdown 报告） |
| DELETE | `/evaluation/dataset/{dataset_id}` | 删除数据集                                     |
| GET    | `/evaluation/table`                | 列出表（供出题选表，可按表名模糊搜索）         |

### 通用约定

- **出题与评测都是后台任务**：`POST /evaluation/dataset`（出题）与 `POST /evaluation`（评测）都只投递任务、立即返回
  `task_id`，进度实时写进 Redis 的任务记录（`plugins.redis.Task`），靠轮询 `GET /evaluation/task` 取（见接口 1、2、3）；
  整库采集（`POST /modeling/database/{database_id}/collect`，见「元数据管理」接口 5）同样投递任务，也用这个接口轮询
  ——请求不再挂着等跑完。
- **任务记录**：`meta.type` 是任务类型（`create_dataset` 出题 / `evaluate_dataset` 评测 / `collect_database` 整库采集），
  `meta.inputs` 是投递时的入参，`meta.steps` 是进度帧，`meta.outputs` 是本轮产出；记录最多留 3 天
  （`plugins.redis.TASK_TTL`，写入即续期），过期后轮询就查不到了。
- **任务按用户批量查**：轮询返回的是某个用户的任务列表（`Task.user_id`，当前固定为 `plugins.redis.DEFAULT_USER_ID`
  即 1 号用户，用户体系接进来后从登录态取），一次最多 `plugins.redis.TASK_LIMIT`（20）条、新的在前；这条查询走
  RediSearch 索引，索引由 `plugins.redis.Task.setup()` 在应用与 worker 启动时建好（Redis 需带 RediSearch 模块，
  见 docker-compose 的 redis-stack-server）。
- **进度帧**（`meta.steps` 里的元素）是图上报的一条 `Step`（`plugins/agents/evaluation/schemas.py`）：

  ```json
  {
    "message": "正在生成 10 个问题",
    "status": "running",
    "error": null,
    "dataset_id": null
  }
  ```

  | 字段         | 类型                              | 说明                                                                   |
      |--------------|-----------------------------------|------------------------------------------------------------------------|
  | `message`    | string                            | 给用户看的进度文本                                                     |
  | `status`     | `running` / `success` / `failed`   | 该帧的执行状态（注意与任务的 `status` 不是同一套取值）                 |
  | `error`      | string / `null`                    | 失败原因，仅 `status` 为 `failed` 时有值                               |
  | `dataset_id` | int / `null`                       | 数据集编号；出题阶段从「数据集已保存」那帧起带上，评测阶段每帧都带     |

  这是出题与评测的帧结构； **采集任务**（`collect_database`）的帧只有 `message` / `status` / `error` 三个字段
  （`plugins.agents.modeling.schemas.Step`，没有 `dataset_id`），帧序见「元数据管理」接口 5。

- 出题 / 评测 / 采集失败不体现为 HTTP 错误码：任务记录落 `status` 为 `error`（`error` 为原因，进度帧里的失败状态叫
  `failed`），
  任务的 `status` 不是 `running` 即代表本轮结束——成功的 `success` 与失败的 `error` 都表示跑完了。
- 任务出意外（连不上库之类）会自动重试：最多 3 次、每次隔 60 秒（见 `plugins.celery.tasks`）。重试期间任务的
  `status` 仍是 `running`，重跑会从第一帧起重新覆盖 `steps`；重试用尽才落 `error`。任务本身的业务失败
  （出题的「表已不存在」「SQL 生成失败」、评测的「数据集为空」之类）不重试。
- 出题是一串大模型请求（以分钟计），评测同样耗时（每条问题跑一遍检索图，评测并发 5）：投递请求本身立即返回，
  耗的是轮询——客户端按几秒一次的间隔查任务接口渲染进度即可，不必给投递接口设长超时。
- 参数校验失败仍是 FastAPI 默认的 `422` JSON 错误体（不是任务）。
- 响应体字段由 `Model.dump()` 生成，`dataset` / `items` / `results` 的字段见接口 5。

### 评测结果报告是 Markdown 正文（`Result.report`）

> 每一轮评测都会生成一份解读报告，以 **Markdown 正文**的形式落库在 `Result.report`，随「查看数据集」接口的
> `results[].report` 原样返回——客户端按 Markdown 渲染（标题、列表、加粗、行内代码）， **不要当 JSON 解析、也不要转义后再展示**。

- **类型与形态**：一个普通字符串（`text`，可为 `null`），内容是纯 Markdown 正文，不是 JSON、不是富文本结构、不是 HTML。
- **允许的 Markdown 元素**：小标题（`#` / `##`）、有序与无序列表、加粗、以及用反引号包裹的 SQL 片段等行内格式。
- **不包含**：代码块围栏与表格——提示词明确禁用了这两者（见 `plugins.agents.evaluation.prompts.evaluation_review.md`），
  故前端无需处理围栏剥离与表格样式。
- **内容结构**：整体结论 → 逐条归因（哪些问题答得不好、大致为什么）→ 共性问题 → 改进建议（见评估图的 `evaluate_dataset` 与
  `EVALUATION_REVIEW_PROMPT`）。
- **生成时机与失败**：报告在「评测完成」之后生成、与结果一起落库，因此 **要等这一轮评测任务收尾**（任务
  `status` 不再是 `running`：`success` 或 `error`）才读得到；报告生成失败会先推一帧 `解读报告生成失败：…`
  （`status` 为 `failed`）并把该字段写为 `null`，但 **不影响这一轮的 `mean` 与逐条 `results[].items`**。
- **多次评测**：数据集每评估一轮就落一条 `Result`，各自带一份独立的报告；接口 5 按编号倒序返回（最新一轮在最前）。

### 接口明细

#### 1. 新建数据集（出题）

- **方法/URL**：`POST /evaluation/dataset`
- **入参 Body**（`DatasetCreate`）：

| 字段          | 类型     | 必填 | 约束                                                            |
|---------------|----------|------|-----------------------------------------------------------------|
| `table_ids`   | string[] | 是   | 1–10 个，出题所用的表编号（`Table.id`，形如 `<库编号>.<表名>`） |
| `size`        | int      | 是   | `ge=1`，生成的问题数量                                          |
| `database_id` | int      | 是   | `ge=1`，出题所用业务库编号（`Database.id`）                     |

```json
{
  "table_ids": [
    "1234567890123456789.order",
    "1234567890123456789.order_item"
  ],
  "size": 10,
  "database_id": 1234567890123456789
}
```

- **出参**：`202`，`application/json`——任务记录已经落 Redis、任务已投进队列，出题还没开始：

```json
{
  "task_id": 1234567890123456791,
  "thread_id": 1234567890123456790
}
```

| 字段        | 类型 | 说明                                                                        |
|-------------|------|-----------------------------------------------------------------------------|
| `task_id`   | int  | 任务编号（雪花 ID）：据此轮询接口 3 看进度与拿数据集编号                    |
| `thread_id` | int  | 本轮会话编号（雪花 ID）：出题跑到数据集落库后，用它恢复评测；也记在数据集上 |

- **进度**：不在本接口返回，轮询 `GET /evaluation/task`（接口 3）取。典型一轮的进度帧顺序：

  | 帧 `message`                             | `status`                  |
      | --------------------------------------- | ------------------------- |
  | 正在生成 {size} 个问题                         | `running`                 |
  | 问题生成完成：共 N 个                            | `success`                 |
  | 正在生成 SQL                                | `running`                 |
  | SQL 生成完成：共 N 条                          | `success`                 |
  | 已组装 N 条问题-SQL 对                         | `success`                 |
  | 正在校验 SQL（每 5 条一批）                       | `running`                 |
  | 收集到 M 条报错的 SQL，正在纠正 / SQL 校验通过：N 条均无需纠正 | `running` / `success`     |
  | SQL 纠正完成：共纠正 M 条（仅需纠正时）                 | `success`                 |
  | 正在保存数据集                                 | `running`                 |
  | 数据集已保存：共 N 条                            | `success`（带 `dataset_id`） |

- **说明**：出题不在请求里跑，而是交给后台的 Celery worker（任务见 `plugins.celery.tasks.create_dataset`）：请求
  只校验表、生成任务与会话编号、写好任务记录（入参齐、步骤空）、投递任务，随即返回 `task_id`。会话编号由服务端
  生成（雪花 ID），图跑到数据集落库即停住（不在这一步评测），之后带上它恢复评测（见接口 2）。出题所用的表由
  `table_ids` 指定（最多 10 张——每张表的字段与取值都要进提示词，再多上下文会撑爆、也更容易触发限流），连同字段
  与取值一并作为库上下文落进数据集的 `reference_contexts`（出题与评测判定都用它）。出题失败（生成、纠正任一步）
  不抛异常，任务记录落 `status` 为 `error`，原因在 `error` 与最后一帧里（见接口 3）。
- **错误**：`404` `指定的表不存在`（`table_ids` 里有元数据库里没有的表编号，在投递任务之前返回）；
  `422` 参数校验失败（JSON 错误体）
- **依赖**：任务由应用启动时拉起的 Celery worker 执行（见 `plugins.celery.CeleryWorker`）；worker 没起来时任务
  只排队、不执行，轮询会一直停在 `running`。任务记录写在 Redis（见 `plugins.redis.Task`），Redis 不可用时本接口
  直接失败（出题进度没有别的落点）。

#### 2. 评估数据集

- **方法/URL**：`POST /evaluation`（数据集编号在请求体里，路径上不再带 `dataset_id`）
- **入参 Body**（`DatasetEvaluation`）：

| 字段          | 类型 | 必填 | 约束                                                                                     |
|---------------|------|------|------------------------------------------------------------------------------------------|
| `dataset_id`  | int  | 是   | `ge=1`，被评测的数据集编号（雪花 ID）                                                    |
| `database_id` | int  | 是   | `ge=1`，本次评测的业务库编号：检索智能体在该库上答题，字段与取值都在该库的向量集合里召回 |

```json
{
  "dataset_id": 1234567890123456789,
  "database_id": 1234567890123456789
}
```

- **出参**：`202`，`application/json`——任务已经投进队列，评测还没开始：

```json
{
  "task_id": 1234567890123456792
}
```

| 字段      | 类型 | 说明                                             |
|-----------|------|--------------------------------------------------|
| `task_id` | int  | 任务编号（雪花 ID）：据此轮询接口 3 看进度与产出 |

- **进度**：不在本接口返回，轮询 `GET /evaluation/task`（接口 3）取。典型一轮的进度帧顺序：

  | 帧 `message`            | `status`  | `dataset_id` |
      | ---------------------- | --------- | ------------ |
  | 正在评测 N 条问题（并发 5）       | `running` | 有            |
  | 评测进度：k/N（逐条上报，凭完成顺序）   | `running` | 有            |
  | 评测完成：SQL 等效率 0.xxxx    | `success` | 有            |
  | 正在生成评测解读报告             | `running` | 有            |
  | 解读报告生成失败：…（仅在报告生成失败时）  | `failed`  | 有            |
  | 评测结果已保存：SQL 等效率 0.xxxx | `success` | 有（收尾）        |

- **说明**：评测同样不在请求里跑，而是交给后台的 Celery worker（任务见 `plugins.celery.tasks.evaluate_dataset`）：请求只
  校验数据集、取它的会话编号、写好任务记录（`meta.type` 为 `evaluate_dataset`）、投递任务，随即返回 `task_id`。
  worker 按数据集的会话编号恢复那一轮运行、跑一轮评测并落一条结果（`Result` 挂在数据集下），故数据集可以反复评估——
  每投递一次就是一个独立的任务（各跑一轮、各落一条结果，上一轮已跑完时先把状态标回「纠正完成」再跑）。
  出题所用的表不用再传：表编号已经在会话状态里（见 `Evaluator.invoke` 的 `table_ids`）。
- **产出**：结果落库后，任务记录里的 `meta.outputs` 会带上 `result_id`（本轮结果编号）与 `mean`（SQL 等效率）；
  这一轮的 **Markdown 解读报告**随结果落库，用接口 5 从 `results[].report` 取（形态与渲染要求见
  「评测结果报告是 Markdown 正文」）。报告生成失败时，帧序里会多一条 `解读报告生成失败：…`（`status` 为 `failed`），
  `report` 为 `null`，但 `mean` 与逐条 `results[].items` 照常落库，不影响这一轮评测结果本身。
- **依赖**：与出题同一个 worker（见接口 1）；任务记录同样最多留 3 天（见 `plugins.redis.TASK_TTL`）。
- **错误**：`404` `指定的数据集不存在`；`422` 参数校验失败（JSON 错误体）

#### 3. 轮询任务进度

- **方法/URL**：`GET /evaluation/task`
- **入参**：无（当前按固定用户批量查，用户编号见「通用约定」）
- **出参**：`200`，数组，元素为任务快照，新的在前（编号倒序），最多 `TASK_LIMIT`（20）条：

```json
[
  {
    "id": 1234567890123456791,
    "user_id": 1,
    "status": "running",
    "error": null,
    "meta": {
      "type": "create_dataset",
      "inputs": {
        "thread_id": 1234567890123456790,
        "table_ids": [
          "1234567890123456789.order"
        ],
        "size": 10,
        "database_id": 1234567890123456789
      },
      "steps": [
        {
          "message": "正在生成 10 个问题",
          "status": "running",
          "error": null,
          "dataset_id": null
        },
        {
          "message": "数据集已保存：共 10 条",
          "status": "success",
          "error": null,
          "dataset_id": 1234567890123456789
        }
      ],
      "outputs": {
        "dataset_id": 1234567890123456789
      }
    }
  }
]
```

| 字段           | 类型                            | 说明                                                                                                                                                     |
|----------------|---------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `id`           | int                             | 任务编号（雪花 ID），也是任务记录的主键                                                                                                                  |
| `user_id`      | int                             | 任务所属用户编号（当前固定为 1）                                                                                                                         |
| `status`       | `running` / `success` / `error` | 任务状态：`running` 还在跑（排队、执行中或重试中），另两个即本轮已结束                                                                                   |
| `error`        | string / `null`                 | 失败原因，仅 `status` 为 `error` 时有值                                                                                                                  |
| `meta.type`    | string                          | 任务类型：`create_dataset`（出题，接口 1）/ `evaluate_dataset`（评测，接口 2）/ `collect_database`（整库采集，「元数据管理」接口 5）                     |
| `meta.inputs`  | object                          | 投递时的入参：出题是 `thread_id`、`table_ids`、`size`、`database_id`；评测是 `dataset_id`、`thread_id`、`database_id`；采集是 `database_id`              |
| `meta.steps`   | Step[]                          | 到此刻为止的进度帧，顺序即发生顺序（结构见「通用约定」）                                                                                                 |
| `meta.outputs` | object                          | 本轮产出：出题是 `dataset_id`（落库那一刻起就有）；评测是 `result_id`、`mean`（结果落库之后才有）；采集是 `database_id`、`collected`（采集成功之后才有） |

- **说明**：`status` 就是判断是否继续轮询的依据——`running` 就隔一会儿再查，`success` / `error` 即本轮结束；
  不必自己去看 `steps` 最后一帧（那一帧的失败状态叫 `failed`，与任务的 `error` 不是同一套取值）。`meta.steps`
  每轮都是全量返回（含 `meta.outputs` 的当前值），客户端直接整体替换即可。产出不必等收尾：出题的数据集编号
  随「数据集已保存」那一帧带上，落库那一刻就能拿到，据此即可去查看数据集（接口 5）、或调评测接口（接口 2）。
- **多任务**：一次给出一批任务（同一用户的最近 20 个），客户端拿自己投递的 `task_id` 在这批里对号入座即可，
  不必逐个去问；任务记录最多留 3 天（见 `plugins.redis.TASK_TTL`），过期后不再出现在列表里——就像这个任务
  从没存在过，要重跑就另起一个任务。

#### 4. 列出数据集

- **方法/URL**：`GET /evaluation/dataset`
- **入参**：无
- **出参**：`200`，数组，元素为 `{id, name}`，新数据集在前（编号是雪花 ID，递增）。

```json
[
  {
    "id": 1234567890123456789,
    "name": "数据集 2026-10-14 10:00:00"
  }
]
```

- **说明**：列表里不含会话编号 `thread_id`、生成失败原因 `error` 与库上下文 `reference_contexts`——它们只在查看单条数据集时才有意义。

#### 5. 查看数据集

- **方法/URL**：`GET /evaluation/dataset/{dataset_id}`
- **入参**：

| 名称         | 位置 | 类型 | 必填 | 说明                  |
|--------------|------|------|------|-----------------------|
| `dataset_id` | Path | int  | 是   | 数据集编号（雪花 ID） |

- **出参**：`200`，数据集本体、条目（问题与参考 SQL）与历次评测结果；三次查询在同一事务里取一致快照。
  每轮评测结果里的 `report` 就是该轮的 **Markdown 解读报告**（直接渲染，不做 JSON 解析）。
  报告中引用的 SQL 是行内反引号片段，报告本身不含代码块围栏与表格（见「评测结果报告是 Markdown 正文」）。

```json
{
  "dataset": {
    "id": 1234567890123456789,
    "thread_id": 1234567890123456790,
    "name": "数据集 2026-10-14 10:00:00",
    "reference_contexts": [
      {
        "id": "1234567890123456789.order",
        "name": "order",
        "role": "fact",
        "description": "订单表",
        "columns": [
          {
            "id": "1234567890123456789.order.status",
            "name": "status",
            "type": "varchar(32)",
            "description": "订单状态",
            "alias": [
              "状态"
            ],
            "role": "dimension",
            "values": [
              "paid",
              "cancelled"
            ]
          }
        ]
      }
    ],
    "error": null
  },
  "items": [
    {
      "id": 1234567890123456791,
      "question": "订单总数是多少？",
      "reference": "SELECT COUNT(*) FROM `order`",
      "dataset_id": 1234567890123456789
    }
  ],
  "results": [
    {
      "id": 1234567890123456792,
      "mean": 0.8,
      "report": "## 整体结论\n\n本轮 10 条问题中 8 条等效（等效率 0.8）。\n\n## 逐条归因\n\n- 订单总数是多少？：生成 SQL 用 COUNT(1)，与参考 SQL 等效。\n\n## 共性问题\n\n- 多表连接的问题容易漏掉 order_item。\n\n## 改进建议\n\n- 在字段描述里补充外键关系说明。",
      "retrieved_contexts": [
        {
          "id": "1234567890123456789.order",
          "name": "order",
          "role": "fact",
          "description": "订单表",
          "columns": [
            {
              "id": "1234567890123456789.order.status",
              "name": "status",
              "type": "varchar(32)",
              "description": "订单状态",
              "alias": [
                "状态"
              ],
              "role": "dimension",
              "values": [
                "paid",
                "cancelled"
              ]
            }
          ]
        }
      ],
      "error": null,
      "dataset_id": 1234567890123456789,
      "items": [
        {
          "id": 1234567890123456793,
          "reference": "SELECT COUNT(*) FROM `order`",
          "answer": "SELECT COUNT(1) FROM `order`",
          "score": 1.0,
          "reason": "两条 SQL 等效",
          "result_id": 1234567890123456792
        }
      ]
    }
  ]
}
```

字段说明：

| 对象              | 字段                                                                                                                                                                                                                                                                                              |
|-------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `dataset`         | `id`、`thread_id`（会话编号）、`name`、`reference_contexts`（出题与判定所用的库上下文：表 / 字段 / 取值）、`error`（出题失败原因）                                                                                                                                                                |
| `items`           | `id`、`question`、`reference`（参考 SQL）、`dataset_id`；按编号升序                                                                                                                                                                                                                               |
| `results`         | `id`、`mean`（平均等效率）、`report`（**该轮的解读报告，Markdown 正文文本**，见上文「评测结果报告是 Markdown 正文」）、`retrieved_contexts`（本轮检索智能体检索到的表信息：表 / 字段 / 取值，逐条问题的依次排开、不去重）、`error`（整轮失败原因）、`dataset_id`、`items`；按编号倒序（新的在前） |
| `results[].items` | `id`、`reference`（参考答案）、`answer`（检索生成的 SQL，未生成或没通过校验时为 `null`）、`score`（等效 1.0 / 不等效或未生成 0.0）、`reason`（得分理由或失败原因）、`result_id`                                                                                                                   |

- **错误**：`404` `指定的数据集不存在`

#### 6. 删除数据集

- **方法/URL**：`DELETE /evaluation/dataset/{dataset_id}`
- **入参**：

| 名称         | 位置 | 类型 | 必填 | 说明                  |
|--------------|------|------|------|-----------------------|
| `dataset_id` | Path | int  | 是   | 数据集编号（雪花 ID） |

- **出参**：`204`，无响应体
- **错误**：`404` `指定的数据集不存在`
- **说明**：条目与历次评测结果随外键级联一起删（`models.dataset` 的 `CASCADE`）；同时清掉这一轮的会话记忆
  （PostgreSQL 里的 checkpoint 与 Redis 里的缓存副本，见 `Evaluator.forget`），之后带同一会话编号再发请求会当成全新的一轮。

#### 7. 列出表

- **方法/URL**：`GET /evaluation/table`
- **入参（Query）**：

| 名称          | 类型   | 必填 | 约束     | 说明                                                                            |
|---------------|--------|------|----------|---------------------------------------------------------------------------------|
| `database_id` | int    | 是   | `ge=1`   | 业务库编号（`Database.id`）                                                     |
| `name`        | string | 否   | 最长 128 | 表名关键字，不区分大小写的子串匹配（`LIKE '%name%'`）；不传或只传空白视作不过滤 |

- **出参**：`200`，数组，元素为 `Table`，按 `name` 升序。

```json
[
  {
    "id": "1234567890123456789.order",
    "name": "order",
    "role": "fact",
    "description": "订单表",
    "database_id": 1234567890123456789
  }
]
```

- **错误**：`404` `指定的数据库不存在`（库未登记；库下没有表时返回 `200` 与空数组）
- **说明**：供新建数据集时选表用——列表里的 `id` 即「新建数据集」的 `table_ids`；表与出题所用业务库同属一家，
  故按 `database_id` 查。关键字里的 `%` 与 `_` 会被当作 LIKE 的通配符参与匹配，本接口只用于选表，不能拿它做唯一性判断。

## 检索问答（`/retrieval`）

把自然语言问题转成 SQL 并在业务库上执行，整条检索链路的进度与结果以 SSE 逐帧返回
（见 `plugins.agents.retrieval.graph.Retriever`）。与出题 / 评测不同，这里 **不落任务记录**：请求挂着这条流，
跑完即结束。

### 接口一览

| 方法 | URL          | 说明                                       |
|------|--------------|--------------------------------------------|
| POST | `/retrieval` | 提问：跑一轮检索（问题 → SQL → 执行），SSE |

### 通用约定

- 返回 `text/event-stream`，每帧形如 `data: {json}\n\n`，`json` 为一条 `Step`（`plugins/agents/retrieval/schemas.py`）：

  ```json
  {
    "message": "SQL 执行完成",
    "status": "success",
    "error": null,
    "sql": "SELECT COUNT(*) FROM `order`",
    "data": [{ "COUNT(*)": 42 }],
    "table_infos": []
  }
  ```

  | 字段          | 类型                          | 说明                                                             |
      |---------------|-------------------------------|------------------------------------------------------------------|
  | `message`     | string                        | 该节点正在做的工作，节点的开始与结束各上报一条                   |
  | `status`      | `running` / `success` / `error` | 帧状态（注意：失败叫 `error`，与评测图进度帧的 `failed` 不同）  |
  | `error`       | string / `null`               | 失败原因，仅 `status` 为 `error` 时有值                          |
  | `sql`         | string / `null`               | 该节点产出的 SQL（生成、校正、校验通过、执行完成时携带）         |
  | `data`        | any / `null`                  | 查询结果，仅执行成功那一帧携带                                   |
  | `table_infos` | `TableInfo[]` / `null`        | 本次检索到的表 / 字段 / 取值（重排后的最终片段），与结果帧一起交出 |

- 失败不体现为 HTTP 错误码：整轮以 `status` 为 `error`（`error` 为原因）的帧结束，HTTP 状态仍是 `200`；
  收到 `error` 帧、或流正常关闭即代表本轮结束。
- 只有参数校验失败是 FastAPI 默认的 `422` JSON 错误体（不是 SSE）。
- `database_id` **不做存在性校验**：库没登记、或还没采集过时召回为空，最终以「没有候选表，无法生成 SQL」
  这类错误帧结束（不像 `/modeling`、`/evaluation` 那样返回 `404`）。
- 过滤节点（`filter_tables` / `filter_metrics`）当前不在链路上，帧序里不会出现「正在过滤数据表 / 指标」。

### 接口明细

#### 1. 提问（自然语言 → SQL 并执行）

- **方法/URL**：`POST /retrieval`
- **入参 Body**（`Question`）：

| 字段          | 类型   | 必填 | 约束                                                                                |
|---------------|--------|------|-------------------------------------------------------------------------------------|
| `content`     | string | 是   | 至少 1 个字符，待查询的自然语言问题                                                 |
| `database_id` | int    | 是   | 本次检索的业务库编号（`Database.id`）：字段与取值都只在该库的向量集合里召回，不跨库 |

```json
{
  "content": "上个月华东区的订单总额是多少？",
  "database_id": 1234567890123456789
}
```

- **出参**：`200`，`text/event-stream`。典型一轮的帧序列（完整模式）：

| 帧 `message`                                               | `status`                        | 附带字段                              |
|------------------------------------------------------------|---------------------------------|---------------------------------------|
| 正在进行意图识别 / 意图识别完成                            | `running` / `success`           |                                       |
| 正在提取关键词 / 关键词提取完成                            | `running` / `success`           |                                       |
| 正在召回字段信息 / 字段信息召回完成                        | `running` / `success`           |                                       |
| 正在召回指标信息 / 指标信息召回完成                        | `running` / `success`           |                                       |
| 正在召回字段取值 / 字段取值召回完成                        | `running` / `success`           |                                       |
| 正在重排候选表与指标 / 重排完成                            | `running` / `success`           |                                       |
| 正在补充数据库与日期信息 / 数据库与日期信息补充完成        | `running` / `success`           |                                       |
| 正在生成 SQL / SQL 生成完成                                | `running` / `success`           | 完成帧带 `sql`                        |
| 正在校验 SQL / SQL 校验通过                                | `running` / `success`           | 通过帧带 `sql`                        |
| SQL 校验未通过 → 正在校正 SQL → SQL 校正完成（→ 回到校验） | `error` → `running` → `success` | 校正帧带 `sql`                        |
| 正在执行 SQL / SQL 执行完成                                | `running` / `success`           | 完成帧带 `sql`、`data`、`table_infos` |

- **结果帧**：`SQL 执行完成` 那一帧同时带 `sql`、`data`（查询结果）与 `table_infos`（本次检索到的表 / 字段 /
  取值，即重排后最终用于生成这条 SQL 的片段）。在此之前帧里出现的 `sql` 只是候选，可能被后续校正替换。
- **失败帧**：意图识别失败（问题不适合查询，理由即 `error`）、SQL 生成 / 校验失败、SQL 执行失败都以 `status`
  为 `error` 的帧结束。校验未通过或执行失败都会回到校正重来（重新生成 → 校验 → 执行），累计校正超过 5 次
  （`plugins.agents.retrieval.nodes.MAX_RETRY`）才以错误帧收场。
- **说明**：除 `content` 与 `database_id` 外没有别的入参——大模型、嵌入模型与重排模型都是应用内配置
  （见 `plugins.agents.retrieval.graph`）；本接口走完整模式（含意图识别与模型合规校验），评估内部走的
  「评估模式」（跳过意图识别、只做 EXPLAIN 校验、不执行 SQL）不对 HTTP 暴露。
