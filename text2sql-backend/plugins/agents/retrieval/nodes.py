from asyncio import gather
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph.graph import END
from jieba.analyse import extract_tags
from loguru import logger
from tortoise.transactions import in_transaction

from models import get_db_info, explain_sql, execute_raw_sql
from models.meta import Column, ColumnRoleOption, Table
from plugins.agents.models import invoke
from plugins.agents.retrieval.schemas import Context, Mode, State, Step, StepStatus, Intention, SQLValidation, \
    KeywordList, FilteredTableList, ColumnInfo, MetricInfo, TableInfo, DatabaseInfo, DateInfo
from plugins.agents.retrieval.prompts import INTENTION_ANALYSIS_PROMPT, COLUMN_KEYWORD_EXTENSION_PROMPT, \
    METRIC_KEYWORD_EXTENSION_PROMPT, VALUE_KEYWORD_EXTENSION_PROMPT, TABLE_FILTERING_PROMPT, \
    METRIC_FILTERING_PROMPT, SQL_GENERATION_PROMPT, SQL_CORRECTION_PROMPT, \
    SQL_VALIDATION_PROMPT, TABLE_RERANK_INSTRUCT, METRIC_RERANK_INSTRUCT
from plugins.json import dump
from plugins.qdrant import Qdrant
from plugins.agents.rerank import RerankClient, RerankResult

# 元数据模型挂在 meta 连接上，多库配置下 in_transaction 需显式指定连接名
META_CONNECTION = "meta"
# 向量库与重排客户端无状态，可常驻模块；LLM、嵌入模型与重排模型随图上下文注入，不在此持有
qdrant = Qdrant()
rerank_client = RerankClient()


async def analyse_intention(state: State, runtime: Runtime[Context]):
    # 评估模式跳过意图识别：不调用模型，直接进入关键词提取
    if state.mode == Mode.EVALUATION:
        logger.info("analyse_intention: evaluation mode, skip intention analysis")
        return Command(goto="extract_keywords")

    writer = runtime.stream_writer
    writer(Step(message="正在进行意图识别", status=StepStatus.RUNNING))
    logger.info(f"analyse_intention: question={state.question!r}")
    intention, error = await invoke(runtime.context.model, [
        SystemMessage(content=INTENTION_ANALYSIS_PROMPT),
        HumanMessage(content=state.question)
    ], Intention)
    if error:
        logger.error(error)
        writer(Step(message="意图识别失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    logger.info(f"analyse_intention: continued={intention.continued}, reason={intention.reason}")
    if not intention.continued:
        # 模型判定问题不适合查询，把它给的理由作为失败原因：既随失败帧推给前端，也写进 state.error
        error = intention.reason or "问题不适合生成查询"
        writer(Step(message="意图识别失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    writer(Step(message="意图识别完成", status=StepStatus.SUCCESS))
    return Command(goto="extract_keywords")


allowed_pos = (
    "n",
    "nr",
    "ns",
    "nt",
    "nz",
    "v",
    "vn",
    "a",
    "an",
    "eng",
    "i",
    "l",
    "m",
    "t"
)


async def extract_keywords(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在提取关键词", status=StepStatus.RUNNING))
    question = state.question
    keywords = list(set(extract_tags(question, allowPOS=allowed_pos) + [question]))
    logger.info(f"extract_keywords: {len(keywords)} keyword(s) -> {keywords}")
    writer(Step(message="关键词提取完成", status=StepStatus.SUCCESS))
    return Command(goto="recall_columns", update={"keywords": keywords})


def build_column_info(column: Column, values: list[Any] | None = None) -> ColumnInfo:
    """把元数据库的字段转成检索阶段的字段信息。

    只取检索上下文需要的字段：元数据库的示例值（examples）与同步标记不进上下文，
    取值（values）由取值召回按需填充，可为空。
    """
    return ColumnInfo(
        id=column.id,
        name=column.name,
        type=column.type,
        description=column.description,
        alias=column.alias,
        table_id=column.table_id,
        role=column.role, values=values
    )


def build_table_info(table: Table, columns: list[ColumnInfo] | None = None) -> TableInfo:
    """把元数据库的表转成检索阶段的表信息。

    与 build_column_info 同一口径：只取上下文需要的字段，元数据库上后加的字段
    （如所属数据库 database_id）不进上下文，列的展开与否由调用方通过 columns 指定。
    """
    return TableInfo(
        id=table.id,
        name=table.name,
        role=table.role,
        description=table.description,
        columns=columns
    )


async def recall_columns(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在召回字段信息", status=StepStatus.RUNNING))
    context = runtime.context
    question = state.question
    logger.info(f"recall_columns: question={question}")
    keywords, error = await invoke(context.model, [
        SystemMessage(content=COLUMN_KEYWORD_EXTENSION_PROMPT),
        HumanMessage(content=f"```text\n{question}\n```")
    ], KeywordList)
    if error:
        logger.error(error)
        writer(Step(message="字段信息召回失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    logger.info(f"recall_columns: extension keywords={keywords}")
    keywords = list(set(keywords + (state.keywords or [])))
    columns = await qdrant.find_columns(state.database_id, keywords, context.embedding_model)
    logger.info(f"recall_columns: recalled column(s) -> {[(x.table_id, x.name) for x in columns]}")
    tables = await Table.filter(id__in=[x.table_id for x in columns]).all()

    # 召回到的字段按表 id 分组保存
    column_infos: dict[str, list[ColumnInfo]] = {}
    for column in columns:
        column_infos.setdefault(column.table_id, []).append(build_column_info(column))

    # 召回到的表按表 id 去重保存，此阶段列信息由 column_infos 承载，故 columns 留空
    table_infos = {table.id: build_table_info(table) for table in tables}
    logger.info(f"recall_columns: recalled {len(table_infos)} table(s), "
                f"{sum(len(group) for group in column_infos.values())} column(s)")
    writer(Step(message="字段信息召回完成", status=StepStatus.SUCCESS))
    return Command(
        goto="recall_metrics",
        update={"column_infos": column_infos, "table_infos": table_infos}
    )


async def recall_metrics(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在召回指标信息", status=StepStatus.RUNNING))
    question = state.question
    logger.info(f"recall_metrics: question={question}")
    keywords, error = await invoke(runtime.context.model, [
        SystemMessage(content=METRIC_KEYWORD_EXTENSION_PROMPT),
        HumanMessage(content=f"```text\n{question}\n```")
    ], KeywordList)
    if error:
        logger.error(error)
        writer(Step(message="指标信息召回失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    logger.info(f"recall_metrics: extension keywords={keywords}")
    keywords = list(set(keywords + (state.keywords or [])))
    metrics = await qdrant.find_metrics(keywords, runtime.context.embedding_model)
    metric_infos = [MetricInfo(**x.dump()) for x in metrics]
    logger.info(f"recall_metrics: recalled {len(metrics)} metric(s)")
    writer(Step(message="指标信息召回完成", status=StepStatus.SUCCESS))
    return Command(goto="recall_values", update={"metric_infos": metric_infos})


async def recall_values(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在召回字段取值", status=StepStatus.RUNNING))
    question = state.question
    logger.info(f"recall_values: question={question}")
    keywords, error = await invoke(runtime.context.model, [
        SystemMessage(content=VALUE_KEYWORD_EXTENSION_PROMPT),
        HumanMessage(content=f"```text\n{question}\n```")
    ], KeywordList)
    if error:
        logger.error(error)
        writer(Step(message="字段取值召回失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    logger.info(f"recall_values: extension keywords={keywords}")
    values = await qdrant.find_values(state.database_id, keywords)
    logger.info(f"recall_values: recalled value(s) -> {[(x.column_id, x.payload) for x in values]}")
    logger.info(f"recall_values: recalled {len(values)} value(s)")

    # 取值按所属字段 id 归组，稍后挂到对应字段的 values 上
    values_infos = {}
    for value in values:
        values_infos.setdefault(value.column_id, []).append(value.payload)

    # 已召回的字段按表 id 分组
    column_infos = {x: list(y) for x, y in (state.column_infos or {}).items()}
    async with in_transaction(META_CONNECTION):
        # 根据召回取值的 column_id 召回对应字段
        relevant_columns = await Column.filter(id__in=set(values_infos)).all()
        # 根据字段所属表的 id 召回该表的主键与外键字段
        table_ids = set(column_infos) | {x.table_id for x in relevant_columns}
        columns = await Column.filter(
            table_id__in=list(table_ids),
            role__in=[ColumnRoleOption.PRIMARY_KEY, ColumnRoleOption.FOREIGN_KEY]
        ).all() + relevant_columns
        tables = await Table.filter(id__in=list(table_ids)).all()

    # 字段按表 id 合并进 column_infos，按字段 id 去重
    for column in columns:
        group = column_infos.setdefault(column.table_id, [])
        if column.id not in {x.id for x in group}:
            group.append(build_column_info(column))

    # 取值按 column_id 挂到对应字段上（含前面节点已召回的字段），未召回到取值的字段为空列表
    for group in column_infos.values():
        for column_info in group:
            column_info.values = values_infos.get(column_info.id, [])

    # 直接组装：此节点之前 columns 必为空，取分组字段即可
    table_infos = {table.id: build_table_info(table, column_infos[table.id]) for table in tables}
    logger.info(f"recall_values: recalled {len(values)} value(s), assembled {len(table_infos)} table(s)")
    writer(Step(message="字段取值召回完成", status=StepStatus.SUCCESS))
    # 暂时跳过过滤节点：直接进重排，恢复时把 goto 改回 "filter_tables"（filter_tables → filter_metrics → rerank）
    return Command(goto="rerank", update={
        "column_infos": column_infos,
        "table_infos": table_infos,
    })


async def filter_tables(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在过滤数据表", status=StepStatus.RUNNING))

    table_infos = state.table_infos or {}
    # 表是生成 SQL 的必要条件，没有候选表则无需调用模型，直接结束
    if not table_infos:
        logger.warning("filter_tables: no candidate table to filter")
        writer(Step(message="数据表过滤失败", status=StepStatus.ERROR, error="没有候选表，无法生成 SQL"))
        return Command(goto=END, update={"error": "没有候选表，无法生成 SQL"})

    payload = {
        "question": state.question,
        "tables": [asdict(x) for x in table_infos.values()],
    }
    filtered_tables, error = await invoke(runtime.context.model, [
        SystemMessage(content=TABLE_FILTERING_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], FilteredTableList)
    if error:
        logger.error(error)
        writer(Step(message="数据表过滤失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    tables = {x.name: set(x.columns) for x in filtered_tables}
    new_table_infos = {}
    for table_info in table_infos.values():
        if table_info.name not in tables:
            continue
        column_options = tables[table_info.name]
        new_columns = []
        for column in table_info.columns or []:
            if column.name in column_options:
                new_columns.append(column)
        table_info.columns = new_columns
        new_table_infos[table_info.id] = table_info
    logger.info(f"filter_tables: kept {len(new_table_infos)} of {len(table_infos)} table(s)")
    writer(Step(message="数据表过滤完成", status=StepStatus.SUCCESS))
    return Command(goto="filter_metrics", update={"table_infos": new_table_infos})


async def filter_metrics(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在过滤指标", status=StepStatus.RUNNING))

    metric_infos = state.metric_infos or []
    # 指标可以为空（问题本身不需要指标），没有候选指标则无需调用模型
    if not metric_infos:
        logger.info("filter_metrics: no candidate metric to filter")
        writer(Step(message="指标过滤完成", status=StepStatus.SUCCESS))
        return Command(goto="rerank", update={"metric_infos": []})

    payload = {
        "question": state.question,
        "metrics": [asdict(x) for x in metric_infos],
    }
    keywords, error = await invoke(runtime.context.model, [
        SystemMessage(content=METRIC_FILTERING_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], KeywordList)
    if error:
        logger.error(error)
        writer(Step(message="指标过滤失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})
    metric_names = set(keywords)
    new_metrics = [x for x in metric_infos if x.name in metric_names]
    logger.info(f"filter_metrics: kept {len(new_metrics)} of {len(metric_infos)} metric(s)")
    writer(Step(message="指标过滤完成", status=StepStatus.SUCCESS))
    return Command(goto="rerank", update={"metric_infos": new_metrics})


# 断崖式截断阈值：相邻分数落差的绝对值下限，以及落差占前一名分数的比值下限
ABSOLUTE_GAP = 0.5
RATIO_GAP = 0.25
# topK 的固定上限：断崖截断之外再压一道上限，避免候选分数扎堆（无断崖）时保留过多
MAX_TOP_K = 10


def top_k(results: list[RerankResult]) -> list[RerankResult]:
    """按相关度降序断崖式取 topK（结果已由重排模型按分数降序返回）。

    从第一名起逐条比较相邻分数，出现断崖即在其前方截断；记 gap 为与前一名的落差、
    previous 为前一名的分数，满足任一条件即为断崖：
    - 落差绝对值：gap >= ABSOLUTE_GAP；
    - 落差比值：gap / previous >= RATIO_GAP。

    故 K 至少为 1：第一名没有前一名可比，始终保留；K 另有固定上限 MAX_TOP_K，
    分数密集而截不出断崖时也不会超过这一上限。
    """
    kept: list[RerankResult] = []
    for result in results:
        # 先判上限：已达上限就不再往下看，断崖与否都不再保留
        if len(kept) >= MAX_TOP_K:
            break
        if kept:
            previous = kept[-1].score
            gap = previous - result.score
            if gap >= ABSOLUTE_GAP or (previous and gap / previous >= RATIO_GAP):
                break
        kept.append(result)
    if len(kept) < len(results):
        logger.info(f"top_k: kept {len(kept)} of {len(results)} candidate(s), "
                    f"scores={[round(x.score, 6) for x in results[:len(kept) + 1]]}")
    return kept


def build_column_text(column: ColumnInfo) -> str:
    """字段的重排文本：字段名 + 别名 + 描述。"""
    alias = "、".join(column.alias or []) or "无"
    description = column.description or "无描述"
    return f"{column.name}（别名：{alias}）：{description}"


def build_table_text(table: TableInfo) -> str:
    """表的重排文本：表名 + 描述 + 字段（字段名、别名与描述用于补充表所承载的业务语义）。"""
    columns = "；".join(build_column_text(x) for x in table.columns or [])
    return f"表名：{table.name}；描述：{table.description}；字段：{columns}"


def build_metric_text(metric: MetricInfo) -> str:
    """指标的重排文本：指标名 + 描述 + 别名。"""
    alias = "、".join(metric.alias or [])
    return f"指标：{metric.name}；描述：{metric.description}；别名：{alias}"


async def rerank(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在重排候选表与指标", status=StepStatus.RUNNING))

    # table_infos 是 dict[表 id, TableInfo]，取 values() 成列表只为对上重排结果里的文档下标
    table_infos = list((state.table_infos or {}).values())
    metric_infos = list(state.metric_infos or [])
    logger.info(f"rerank: question={state.question}, {len(table_infos)} table(s), {len(metric_infos)} metric(s)")

    # 表是生成 SQL 的必要条件（指标可空），没有候选表则无需重排，直接结束
    if not table_infos:
        logger.warning("rerank: no candidate table to rerank")
        writer(Step(message="重排失败", status=StepStatus.ERROR, error="没有候选表，无法生成 SQL"))
        return Command(goto=END, update={"error": "没有候选表，无法生成 SQL"})

    try:
        # 表与指标分别重排，两次请求并发下发；重排模型取自图上下文，客户端本身不绑定模型。
        # 表与指标各带一份指令说明，告诉重排模型文档的构成以及「按问题判断相关性」的排序标准
        table_results, metric_results = await gather(
            rerank_client.rerank(state.question, [build_table_text(x) for x in table_infos],
                                 runtime.context.rerank_model, instruct=TABLE_RERANK_INSTRUCT),
            rerank_client.rerank(state.question, [build_metric_text(x) for x in metric_infos],
                                 runtime.context.rerank_model, instruct=METRIC_RERANK_INSTRUCT)
        )
    except Exception as e:
        logger.error(e)
        writer(Step(message="重排失败", status=StepStatus.ERROR, error=str(e)))
        return Command(goto=END, update={"error": str(e)})

    # 重排结果已按相关度降序，断崖式截断后按表 id 重建字典（插入顺序即重排后的顺序）
    table_results = top_k(table_results)
    metric_results = top_k(metric_results)
    new_table_infos = {table_infos[x.index].id: table_infos[x.index] for x in table_results}
    new_metric_infos = [metric_infos[x.index] for x in metric_results]
    logger.info(f"rerank: kept {len(new_table_infos)} of {len(table_infos)} table(s), "
                f"{len(new_metric_infos)} of {len(metric_infos)} metric(s)")
    writer(Step(message="重排完成", status=StepStatus.SUCCESS))
    return Command(goto="add_extra_info", update={"table_infos": new_table_infos, "metric_infos": new_metric_infos})


zone_info = ZoneInfo("Asia/Shanghai")


async def add_extra_info(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在补充数据库与日期信息", status=StepStatus.RUNNING))
    db_info = DatabaseInfo(**await get_db_info(state.database_id))
    now = datetime.now(zone_info)
    date_info = DateInfo(now=now.strftime("%Y-%m-%d %H:%M:%S"),
                         week_day=now.isoweekday(),
                         quarter=(now.month - 1) // 3 + 1)
    logger.info(f"add_extra_info: now={date_info.now}, week_day={date_info.week_day}, quarter={date_info.quarter}")
    writer(Step(message="数据库与日期信息补充完成", status=StepStatus.SUCCESS))
    return Command(goto="generate_sql", update={"db_info": db_info, "date_info": date_info})


async def generate_sql(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在生成 SQL", status=StepStatus.RUNNING))

    table_infos = state.table_infos or {}
    # 表是生成 SQL 的必要条件，没有表则无从生成，直接结束
    if not table_infos:
        logger.warning("generate_sql: no candidate table in state")
        writer(Step(message="SQL 生成失败", status=StepStatus.ERROR, error="没有候选表，无法生成 SQL"))
        return Command(goto=END, update={"error": "没有候选表，无法生成 SQL"})

    payload = {
        "question": state.question,
        "tables": [asdict(x) for x in table_infos.values()],
        "metrics": [asdict(x) for x in state.metric_infos or []],
        "db_info": asdict(state.db_info) if state.db_info else None,
        "date_info": asdict(state.date_info) if state.date_info else None,
    }
    response = await runtime.context.model.ainvoke([
        SystemMessage(content=SQL_GENERATION_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ])

    sql = response.content
    logger.info(f"generate_sql: {sql}")
    writer(Step(message="SQL 生成完成", status=StepStatus.SUCCESS, sql=sql))
    return Command(goto="validate_sql", update={"sql": sql})


MAX_RETRY = 5


async def validate_sql(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在校验 SQL", status=StepStatus.RUNNING))

    mode = state.mode
    sql = state.sql
    if not sql:
        logger.warning("validate_sql: empty sql")
        writer(Step(message="SQL 校验未通过", status=StepStatus.ERROR, error="SQL is empty"))
        return Command(goto=END, update={"error": "SQL is empty"})

    error = None
    valid, detail = await explain_sql(sql, state.database_id)
    logger.info(f"validate_sql: valid={valid}, error={detail}, retry_count={state.retry_count}, mode={mode}")
    if not valid:
        error = detail or "SQL is invalid"
    elif mode == Mode.EVALUATION:
        # 评估模式只做 EXPLAIN 语法校验，跳过调用大模型的合规校验
        logger.info("validate_sql: evaluation mode, skip model validation")
    else:
        validation, failure = await invoke(runtime.context.model, [
            SystemMessage(content=SQL_VALIDATION_PROMPT),
            HumanMessage(content=sql)
        ], SQLValidation)
        if failure:
            logger.error(failure)
            writer(Step(message="SQL 校验未通过", status=StepStatus.ERROR, error=failure))
            return Command(goto=END, update={"error": failure})

        logger.info(f"validate_sql: continued={validation.continued}, reason={validation.reason}")
        if not validation.continued:
            error = validation.reason or "SQL is not allowed"

    if error is None:
        if mode == Mode.EVALUATION:
            # 评估模式校验通过即整轮结束（跳过执行节点）：这一帧是整轮唯一的结果帧，把校验无误的 SQL 与本次
            # 检索到的表信息一并交出（见 graph.Retriever.eval 与 models.dataset.Result.retrieved_contexts）。
            # 表信息只在这一帧出现：校验没通过就到不了这里，评估侧拿到的 SQL 与表信息都为空
            logger.info("validate_sql: evaluation mode, skip execute_sql")
            writer(Step(
                message="SQL 校验通过",
                status=StepStatus.SUCCESS,
                sql=sql,
                table_infos=list((state.table_infos or {}).values())
            ))
            return Command(goto=END, update={"error": None})
        writer(Step(message="SQL 校验通过", status=StepStatus.SUCCESS, sql=sql))
        return Command(goto="execute_sql", update={"error": None})

    if state.retry_count >= MAX_RETRY:
        logger.warning(f"validate_sql: retry limit exceeded, retry_count={state.retry_count}")
        writer(Step(message="SQL 校验未通过", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    writer(Step(message="SQL 校验未通过", status=StepStatus.ERROR, error=error))
    return Command(goto="correct_sql", update={"error": error})


async def correct_sql(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在校正 SQL", status=StepStatus.RUNNING))

    payload = {
        "question": state.question,
        "sql": state.sql,
        "error": state.error,
        "tables": [asdict(x) for x in (state.table_infos or {}).values()],
        "metrics": [asdict(x) for x in state.metric_infos or []],
        "date_info": asdict(state.date_info) if state.date_info else None,
        "db_info": asdict(state.db_info) if state.db_info else None,
    }
    response = await runtime.context.model.ainvoke([
        SystemMessage(content=SQL_CORRECTION_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ])

    sql = response.content
    logger.info(f"correct_sql: {sql}")
    writer(Step(message="SQL 校正完成", status=StepStatus.SUCCESS, sql=sql))
    return Command(goto="validate_sql", update={"sql": sql, "retry_count": state.retry_count + 1})


async def execute_sql(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在执行 SQL", status=StepStatus.RUNNING))

    sql = state.sql
    if not sql:
        logger.warning("execute_sql: empty sql")
        writer(Step(message="SQL 执行失败", status=StepStatus.ERROR, error="SQL is empty"))
        return Command(goto=END, update={"error": "SQL is empty"})

    try:
        data = await execute_raw_sql(sql, state.database_id)
    except Exception as e:
        logger.error(e)
        writer(Step(message="SQL 执行失败", status=StepStatus.ERROR, error=str(e)))
        return Command(goto="correct_sql", update={"error": str(e)})

    logger.info(f"execute_sql: executed successfully, got {len(data) if data is not None else 0} row(s)")
    # 完整模式的结果帧：与评估模式的校验通过帧同一口径，SQL 与检索到的表片段一起交出
    # （见 graph.Retriever.eval 与 models.dataset.Result.retrieved_contexts）
    writer(Step(message="SQL 执行完成", status=StepStatus.SUCCESS, sql=sql, data=data,
                table_infos=list((state.table_infos or {}).values())))
    return Command(goto=END)
