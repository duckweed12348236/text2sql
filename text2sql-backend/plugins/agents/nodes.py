from dataclasses import asdict, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.embeddings import Embeddings
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from langgraph.graph import END
from jieba.analyse import extract_tags
from loguru import logger
from tortoise.transactions import in_transaction

from models import get_db_info, explain_sql, execute_raw_sql
from models.meta import Column, ColumnRelation, Metric, Table
from plugins.agents.models import invoke
from plugins.agents.schemas import Context, Mode, State, Step, StepStatus, Intention, QuestionRewrite, \
    RecallProbe, SQLValidation, KeywordList, TableIdList, RelationList, MetricMatch, MetricAnswer, \
    ColumnInfo, MetricInfo, TableInfo, DatabaseInfo, DateInfo
from plugins.agents.prompts import INTENTION_ANALYSIS_PROMPT, QUESTION_REWRITE_PROMPT, \
    COLUMN_KEYWORD_EXTENSION_PROMPT, RECALL_PROBE_PROMPT, METRIC_KEYWORD_EXTENSION_PROMPT, \
    TABLE_FILTERING_PROMPT, RELATION_RECOGNITION_PROMPT, METRIC_MATCHING_PROMPT, METRIC_ANSWER_PROMPT, \
    SQL_GENERATION_PROMPT, SQL_CORRECTION_PROMPT, SQL_VALIDATION_PROMPT, \
    TABLE_RERANK_INSTRUCT, METRIC_RERANK_INSTRUCT
from plugins.json import dump
from plugins.qdrant import Qdrant
from plugins.agents.rerank import RerankClient, RerankResult

# 元数据模型挂在 meta 连接上，多库配置下 in_transaction 需显式指定连接名
META_CONNECTION = "meta"
# 向量库与重排客户端无状态，可常驻模块；LLM、嵌入模型与重排模型随图上下文注入，不在此持有
qdrant = Qdrant()
rerank_client = RerankClient()

# 召回指标 / 召回字段前是否先让大模型做一次关键词扩展：暂时关掉——直接用 extract_keywords 出的关键词召回，
# 这两步各少一次模型调用（调用与提示词都保留着，改回 True 即可恢复）
EXTEND_KEYWORDS = False


def build_query_keywords(keywords: list[str] | None, question: str) -> list[str]:
    """提交检索用的关键词：抽出来的词在前，用户问题整句在最后（按出现顺序去重）。

    分词可能切碎业务黑话（"GMV""客单价"），问题整句并进来兜底语义；state.keywords 本身不存问题
    （见 extract_keywords），每次提交时现拼，免得问题被并进状态后又被下游反复拼接。
    """
    return list(dict.fromkeys([x for x in (keywords or []) if x] + [question]))


def build_rerank_query(keywords: list[str] | None, question: str) -> str:
    """重排用的查询文本：把关键词与用户问题拼成一段自然语言。

    关键词单独成句排在前面（补上问题里可能被切碎、切错的业务词汇），用户问题原句在后（给出完整语义），
    重排模型照这段文本判断该选哪张表（见 prompts/table_rerank.md）。没抽到关键词时只留问题。
    """
    keywords = [x for x in (keywords or []) if x]
    if not keywords:
        return f"用户问题：{question}。"
    return f"关键词：{'、'.join(keywords)}。用户问题：{question}。"


async def analyse_intention(state: State, runtime: Runtime[Context]):
    # 评估模式跳过意图识别：不调用模型，直接进入关键词提取（原始问题照记，后面的生成 SQL 要用）
    if state.mode == Mode.EVALUATION:
        logger.info("analyse_intention: evaluation mode, skip intention analysis")
        return Command(goto="extract_keywords", update={"original_question": state.question})

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

    logger.info(f"analyse_intention: continued={intention.continued}, reason={intention.reason}, "
                f"clarification={intention.clarification}")
    if not intention.continued:
        # 过于模糊、定不下查询对象时，模型给一句反问（见 prompts.intention_analysis.md）：走 clarification 字段、
        # 不塞进 error——前端把这句话当提问展示，用户补全问题后重新提问即可。
        # 其余拒绝原因（修改类、无关、超大数据量）没有反问，按失败帧结束
        if intention.clarification:
            writer(Step(message="需要补充问题信息", status=StepStatus.ERROR,
                        clarification=intention.clarification))
            return Command(goto=END, update={"clarification": intention.clarification})
        # 模型判定问题不适合查询，把它给的理由作为失败原因：既随失败帧推给前端，也写进 state.error
        error = intention.reason or "问题不适合生成查询"
        writer(Step(message="意图识别失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    writer(Step(message="意图识别完成", status=StepStatus.SUCCESS))
    # 确认问题适合查询后先改写：把口语化的说法补完整，再交给关键词提取（见 rewrite_question）。
    # 原始问题一并记下（改写前的那一版）：触发重召回后生成 SQL 用的是它，不是临时改写的问题
    return Command(goto="rewrite_question", update={"original_question": state.question})


async def rewrite_question(state: State, runtime: Runtime[Context]):
    """把用户口语化的提问改写成完整明确的问题，再交给关键词提取。

    :param state: 图状态，question 是用户的原话
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model

    排在意图识别之后、关键词提取之前（提示词见 prompts.question_rewrite.md）：用户常说得省略——
    「创新发展中心的员工有哪些」里的「创新发展中心」多半是部门、「员工有哪些」真正想看的是姓名与工号。
    模型据此做两件事：补全实体名称（→「创新发展中心部门」）、补出隐含要看的属性（→「姓名、工号」）。
    改写后的问题替换 state.question，后面的关键词提取、召回、重排与生成 SQL 都按它来，检索因此更容易
    命中正确的表与字段（「部门」「姓名」这些词能直接召回对应的表与列）。

    这是**增强**、不是关卡：模型调用失败、返回空问题、或改写结果与原文一致时，都原样往下走，不因它挡住
    整轮检索。补了哪些实体、推测了哪些属性只记日志（状态里只留改写后的问题）。
    评估模式不经过本节点：出题环节给出的问题本身已经完整明确（见 analyse_intention 的跳过分支）。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在改写问题", status=StepStatus.RUNNING))
    question = state.question
    logger.info(f"rewrite_question: question={question!r}")
    rewrite, error = await invoke(runtime.context.model, [
        SystemMessage(content=QUESTION_REWRITE_PROMPT),
        HumanMessage(content=f"```json\n{dump({'question': question})}\n```")
    ], QuestionRewrite)
    if error:
        logger.warning(f"rewrite_question: {error}，按原问题继续")
        writer(Step(message="问题改写失败，按原问题继续", status=StepStatus.SUCCESS))
        return Command(goto="extract_keywords")

    rewritten = (rewrite.question or "").strip()
    if not rewritten or rewritten == question:
        logger.info("rewrite_question: question is clear enough, keep it")
        writer(Step(message="问题已足够明确", status=StepStatus.SUCCESS))
        return Command(goto="extract_keywords")

    entities = [f"{x.mention}→{x.expanded}（{x.type or '未定'}）" for x in rewrite.entities or []]
    logger.info(f"rewrite_question: {question!r} -> {rewritten!r}；entities={entities}，"
                f"attributes={rewrite.attributes or []}；reason={rewrite.reason}")
    writer(Step(message="问题改写完成", status=StepStatus.SUCCESS))
    return Command(goto="extract_keywords", update={"question": rewritten})


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
    """用 jieba 抽关键词，抽到什么就用什么召回；抽完同时放出指标与字段两路召回。

    词性过滤只留名词 / 动词 / 英文 / 简称等（见 allowed_pos），这条链路不调用大模型：分词结果是关键词的
    唯一来源。用户问题不并进关键词，在下游各自「提交」时并入（召回见 build_query_keywords，重排见
    build_rerank_query）：关键词于是保持干净，状态与日志里看到的都是真正的词，问题也不会被反复并进
    state.keywords。

    两路召回互不依赖（指标只看关键词与问题，字段只看关键词与问题），故一次性派给 recall_metrics 与
    recall_columns 并行跑，再在 rerank_tables 前汇合（join 边见 Retriever.build）。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在提取关键词", status=StepStatus.RUNNING))
    keywords = list(dict.fromkeys(extract_tags(state.question, allowPOS=allowed_pos)))
    logger.info(f"extract_keywords: {len(keywords)} keyword(s) -> {keywords}")
    writer(Step(message="关键词提取完成", status=StepStatus.SUCCESS))
    # 首轮关键词另存一份（见 State.original_keywords）：重召回时用它对照「缺了什么」，
    # state.keywords 则会被补召回换掉（见 generate_sql）
    return Command(goto=["recall_metrics", "recall_columns"],
                   update={"keywords": keywords, "original_keywords": keywords})


async def load_related_refs(column_ids: list[str]) -> dict[str, list[str]]:
    """取这些字段的关联字段引用：{字段编号: ["<表名>.<字段名>", ...]}。

    关系存在 ColumnRelation 中间表（`Column.related_columns` 这个多对多的中间表）里，一行是
    (source, target)：source 的关联列表里有 target。这里一次把这批字段的关系行取回来，再按 target
    回字段表拿表名与列名，拼成 `<表名>.<字段名>`（表名取对端表编号的后半段：表编号形如 <库编号>.<表名>）——
    与 ColumnInfo.related_columns 的格式一致，拼 SQL 时直接照着写 JOIN。
    一批字段只查两次库（关系行 + 对端字段），不在组装时逐字段回表。
    """
    if not column_ids:
        return {}
    rows = await ColumnRelation.filter(source_id__in=column_ids).values_list("source_id", "target_id")
    if not rows:
        return {}
    targets = {target_id: None for _, target_id in rows}
    name_of = {
        column.id: (column.table_id.split(".", 1)[-1], column.name)
        for column in await Column.filter(id__in=list(targets)).only("id", "table_id", "name")
    }
    refs: dict[str, list[str]] = {}
    for source_id, target_id in rows:
        target = name_of.get(target_id)
        if target is None:
            # 对端字段已被删掉（中间表随外键级联清，正常走不到这里）：跳过，不往上下文里塞野数据
            continue
        refs.setdefault(source_id, []).append(f"{target[0]}.{target[1]}")
    return {source_id: sorted(values) for source_id, values in refs.items()}


async def load_table_columns(table_ids: list[str]) -> dict[str, list[ColumnInfo]]:
    """按表编号取回这些表的**全部**字段（含元数据库里的取值样本与可关联字段），按表编号分组。

    :param table_ids: 表编号（Table.id）
    :return: ``{表编号: [字段信息, ...]}``：字段按编号排序，取值样本与关联字段都已带上

    召回阶段用（见 recall_columns）：关键词只召回与问题相关的字段，据此定下候选表之后，把候选表的字段
    补全——重排与细筛都要看整张表（WHERE 条件、JOIN 键都在其中，主键 / 外键自然也在里面），生成 SQL 时
    也常用到没被召回的字段。

    这里的取值只是元数据库里登记的样本（Column.values，采样脚本写的部分取值，见 add_values.py）；
    向量库里的同步取值不在这里召回（细筛之后才取，见 load_synchronized_values 与 filter_tables）。
    """
    if not table_ids:
        return {}
    columns = await Column.filter(table_id__in=list(table_ids)).order_by("id")
    # 关联字段按这批编号一次取回，组装时直接带上
    related_of = await load_related_refs([column.id for column in columns])
    grouped: dict[str, list[ColumnInfo]] = {}
    for column in columns:
        grouped.setdefault(column.table_id, []).append(
            build_column_info(column, values=column.values or [], related=related_of.get(column.id, [])))
    return grouped


async def load_synchronized_values(table_ids: list[str], database_id: int) -> dict[str, list[Any]]:
    """从向量库的 values 集合召回这些表里同步字段（Column.synchronized）的取值。

    :param table_ids: 表编号（Table.id）
    :param database_id: 业务库编号（Database.id）：取值在该库的 values 集合里（一个字段一个点，
        payload 是它的全部取值范围，见 Qdrant.add_values）
    :return: ``{字段编号: [取值, ...]}``

    细筛表之后用（见 filter_tables）：只有真正选中的几张表才去向量库取同步取值，候选表阶段不花这个查询；
    取回的值与元数据库里的样本合并去重后就是「该列在数据库中的取值」（见 merge_values）。
    """
    if not table_ids:
        return {}
    columns = await Column.filter(table_id__in=list(table_ids), synchronized=True).only("id")
    values = await qdrant.find_values(database_id, [column.id for column in columns])
    return {value.column_id: list(value.payload) for value in values}


def merge_values(sample: list[Any] | None, extra: list[Any] | None) -> list[Any]:
    """把一个字段的两处取值合并成一个列表（去重，保持先后顺序）。

    :param sample: 元数据库里登记的取值样本（Column.values，采样脚本写的部分取值，见 add_values.py）
    :param extra: 向量库 values 集合里召回的取值（同步字段的取值范围，见 Qdrant.find_values）
    :return: 合并去重后的取值列表

    两处取值可能都可能有、也可能只有一边有（元数据库的样本是采样的，向量库的取值是同步时取的），
    合并后按「先元数据库、后向量库」的顺序去重：同一取值只留一份，模型看到的就是「该列在数据库里的取值」。
    """
    merged: list[Any] = []
    for value in [*(sample or []), *(extra or [])]:
        if value not in merged:
            merged.append(value)
    return merged


def build_column_info(column: Column, values: list[Any] | None = None,
                      related: list[str] | None = None) -> ColumnInfo:
    """把元数据库的字段转成检索阶段的字段信息。

    只取检索上下文需要的字段：元数据库的同步标记不进上下文；取值（values）是该列在数据库中的取值，
    由调用方按阶段传——召回阶段传元数据库里的样本（见 load_table_columns），细筛之后再把向量库召回的
    同步取值合并进来（见 merge_values 与 filter_tables）；可为空（该列没采过取值）。

    related：该字段的关联字段引用（`<表名>.<字段名>`，见 load_related_refs），由调用方按这批字段
    一次性取好再传进来；这里不查库，免得在循环里逐字段回表。不传时按空列表处理。
    """
    return ColumnInfo(
        id=column.id,
        name=column.name,
        type=column.type,
        description=column.description,
        alias=column.alias,
        table_id=column.table_id,
        role=column.role,
        values=values,
        related_columns=related or []
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


def merge_table_infos(previous: list[TableInfo] | None,
                      current: list[TableInfo] | None) -> list[TableInfo]:
    """把本轮细筛后选中的表并进跨轮累加列表（按表编号去重，保持先后顺序）。

    :param previous: 已累加的候选表（此前各轮细筛选中的结果，见 State.final_table_infos）
    :param current: 本轮细筛后留下的表（见 filter_tables）
    :return: 合并去重后的列表：本轮选中的在前，此前选中、本轮没选上的接在后面

    补召回时会重新召回（见 probe_recall），新关键词未必还能命中上一轮已经找到的表——那几张表直接
    丢掉，后续就只能在一份更差的候选里挑，越查越偏，故把两轮并起来。同一张表两处都在时以本轮为准（字段与
    取值都是重新取的），位置也从本轮的顺序。
    """
    merged: dict[str, TableInfo] = {}
    for table in [*(current or []), *(previous or [])]:
        merged.setdefault(table.id, table)
    return list(merged.values())


async def recall_columns(state: State, runtime: Runtime[Context]):
    """召回字段并据此召回候选表的全部字段，组装成候选表。

    字段按关键词在 columns.<编号> 集合里召回（见 Qdrant.find_columns），关键词只是用来定位候选表；定下表之后
    按表编号把这些表的**全部**字段取回（含元数据库里的取值样本与可关联字段，见 load_table_columns），
    重排与细筛看的是整张表（主键 / 外键、WHERE 与 JOIN 要用的字段自然都在里面）。

    向量库里的同步取值不在这一步召回：细筛表之后才取（见 load_synchronized_values 与 filter_tables），
    候选表阶段只带元数据库里的取值样本。

    组装结果写进 ``table_infos``（**这一轮**的候选表）；细筛之后选中的表才并入 ``final_table_infos``
    （见 filter_tables / merge_table_infos），此后的节点都以累加后的那一份为准。

    与 recall_metrics 并行跑（见 extract_keywords）：字段这一路接着重排、过滤（见 rerank_tables /
    filter_tables），指标那一路同时重排、核对口径，两路最后在 validate_metrics 汇合（join 边见 graph.build）。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在召回字段与取值", status=StepStatus.RUNNING))
    context = runtime.context
    question = state.question
    logger.info(f"recall_columns: question={question}")
    # 关键词扩展暂时关掉（见 EXTEND_KEYWORDS）：直接用上游关键词召回，省一次模型调用。
    # 问题整句在这里并入（见 build_query_keywords）：state.keywords 只有抽出来的词
    keywords = build_query_keywords(state.keywords, question)
    if EXTEND_KEYWORDS:
        extension, error = await invoke(context.model, [
            SystemMessage(content=COLUMN_KEYWORD_EXTENSION_PROMPT),
            HumanMessage(content=f"```text\n{question}\n```")
        ], KeywordList)
        if error:
            logger.error(error)
            writer(Step(message="字段信息召回失败", status=StepStatus.ERROR, error=error))
            return Command(goto=END, update={"error": error})
        logger.info(f"recall_columns: extension keywords={extension}")
        keywords = list(dict.fromkeys([*keywords, *extension]))

    columns = await qdrant.find_columns(state.database_id, keywords, context.embedding_model)
    logger.info(f"recall_columns: {len(columns)} column(s) recalled")
    # 关键词只用来定位候选表：据此把候选表的全部字段取回（含元数据库取值样本与可关联字段）。
    # 向量库里的同步取值不在这里取——细筛之后才取（见 filter_tables / load_synchronized_values）
    table_ids = {column.table_id for column in columns}
    tables = await Table.filter(id__in=list(table_ids)).all()
    grouped = await load_table_columns(list(table_ids))

    table_infos = [build_table_info(table, grouped.get(table.id, [])) for table in tables]
    logger.info(f"recall_columns: recalled {len(table_infos)} table(s), "
                f"{sum(len(columns) for columns in grouped.values())} column(s)")
    writer(Step(message="字段与取值召回完成", status=StepStatus.SUCCESS))
    # 字段这一路继续往下走：重排 → 过滤，两路在 validate_metrics 汇合
    return Command(goto="rerank_tables", update={"table_infos": table_infos})


async def recall_metrics(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在召回指标信息", status=StepStatus.RUNNING))
    question = state.question
    logger.info(f"recall_metrics: question={question}")
    # 关键词扩展暂时关掉（见 EXTEND_KEYWORDS）：直接用 extract_keywords 出的关键词召回，省一次模型调用。
    # 问题整句在这里并入（见 build_query_keywords）：state.keywords 只有抽出来的词
    keywords = build_query_keywords(state.keywords, question)
    if EXTEND_KEYWORDS:
        extension, error = await invoke(runtime.context.model, [
            SystemMessage(content=METRIC_KEYWORD_EXTENSION_PROMPT),
            HumanMessage(content=f"```text\n{question}\n```")
        ], KeywordList)
        if error:
            logger.error(error)
            writer(Step(message="指标信息召回失败", status=StepStatus.ERROR, error=error))
            return Command(goto=END, update={"error": error})
        logger.info(f"recall_metrics: extension keywords={extension}")
        keywords = list(dict.fromkeys([*keywords, *extension]))

    # 只在本次检索的业务库里召回指标：指标与字段 / 取值一样按库分开存（见 Qdrant.find_metrics）
    metrics = await qdrant.find_metrics(state.database_id, keywords, runtime.context.embedding_model)
    metric_infos = [MetricInfo(**x.dump()) for x in metrics]
    logger.info(f"recall_metrics: recalled {len(metrics)} metric(s) in database {state.database_id}")
    writer(Step(message="指标信息召回完成", status=StepStatus.SUCCESS))
    # 与字段那一路并行跑（见 extract_keywords）：这一路先重排，再到 validate_metrics 汇合、核对口径
    return Command(goto="rerank_metrics", update={"metric_infos": metric_infos})


# 断崖式截断阈值：相邻分数落差的绝对值下限，以及落差占前一名分数的比值下限
ABSOLUTE_GAP = 0.5
RATIO_GAP = 0.25
# topK 的固定上限：断崖截断之外再压一道上限，避免候选分数扎堆（无断崖）时保留过多
MAX_TOP_K = 5


def decide_top_k(results: list[RerankResult]) -> list[RerankResult]:
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


async def rerank_metrics(state: State, runtime: Runtime[Context]):
    """重排候选指标：按问题把召回的指标压到最相关的一批（断崖截断，见 pop_top_k）。

    :param state: 图状态，metric_infos 是 recall_metrics 给出的候选指标
    :param runtime: 图运行上下文，重排模型取自 runtime.context.rerank_model

    指标排在字段召回之前：它不依赖字段与取值，先收敛指标，后面几个节点就只带着这批指标走。
    指标可以是空的（问题本身不需要指标）：没有候选时也往下走，交给 validate_metrics 判断问题是否需要指标。
    重排文本见 build_metric_text，指令见 METRIC_RERANK_INSTRUCT（说明文档构成与排序标准）；
    重排模型报错时按失败结束——指标是生成 SQL 的口径依据，拿不到就宁可让这一轮停下来。

    本节点是指标这一路的收口：正常出口只写 metric_infos、不再自己决定下一步，validate_metrics 由 graph.build
    里的 join 边触发（等字段那一路的 filter_tables 也跑完，两路才汇合）。不能用 Command 直跳 validate_metrics：
    它是 join 的汇合点，直跳会绕过等待、让 validate_metrics 提前跑一次，join 之后再跑一次——同一轮口径核对两遍，
    下游 add_extra_info → generate_sql 也跟着跑两遍，重召回时两条链还会互相踩线（表现为重召回后跑到
    filter_tables 就没了后续）。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在重排候选指标", status=StepStatus.RUNNING))
    metric_infos = list(state.metric_infos or [])
    logger.info(f"rerank_metrics: question={state.question}, {len(metric_infos)} metric(s)")
    if not metric_infos:
        logger.info("rerank_metrics: no candidate metric to rerank")
        writer(Step(message="指标重排完成", status=StepStatus.SUCCESS))
        return {"metric_infos": []}

    try:
        results = await rerank_client.rerank(state.question, [build_metric_text(x) for x in metric_infos],
                                             runtime.context.rerank_model, instruct=METRIC_RERANK_INSTRUCT)
    except Exception as e:
        logger.error(e)
        writer(Step(message="指标重排失败", status=StepStatus.ERROR, error=str(e)))
        return Command(goto=END, update={"error": str(e)})

    # 重排结果已按相关度降序，断崖式截断后按原顺序取回（列表顺序即重排后的顺序）
    new_metric_infos = [metric_infos[x.index] for x in decide_top_k(results)]
    logger.info(f"rerank_metrics: kept {len(new_metric_infos)} of {len(metric_infos)} metric(s)")
    writer(Step(message="指标重排完成", status=StepStatus.SUCCESS))
    # 不走 Command：validate_metrics 由 join 边触发，要等字段那一路的 filter_tables 也跑完（见 graph.build 的
    # add_edge）。重排只是按相关度排序，口径对不对由 validate_metrics 核对
    return {"metric_infos": new_metric_infos}


# 指标口径最多重问几次：首次反问之后，回答答不到点子上会换个问法再问，超过这个次数就不再纠缠
MAX_ASK_RETRY = 2


async def validate_metrics(state: State, runtime: Runtime[Context]):
    """过滤重排后的指标：先认出用户输入涉及哪些指标，再看召回里有没有与之一致的，对不上就反问定义。

    :param state: 图状态，metric_infos 是 rerank_metrics 收敛后的指标（可能为空）
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model

    由 join 边触发（见 graph.build）：指标那一路（recall_metrics → rerank_metrics）与字段那一路
    （recall_columns → rerank_tables → filter_tables）并行跑，两路都收口才进本节点，因此这里读到的
    metric_infos 与 final_table_infos 都是完整的。口径一旦理解错，生成的 SQL 全都要重来，故在往下走之前
    先筛一次（提示词见 prompts.metric_matching.md）。
    模型分两步判断：先按用户原话列出问题涉及的指标（required，用用户自己的说法），再逐个比对候选项的
    统计范围、过滤条件与时间口径，对上的给下标（indexes），对不上的留在 missing 里。
    模型只按名称、描述与别名判断口径，payload 里不带指标编号（见 MetricInfo.dump），保留哪几条用**下标**指代
    （同名指标很常见，下标比名字可靠）；`metrics` 为空也照问——没召回到候选时，missing 就是 required 的全部。

    三种结果：
    - 问题不需要指标（明细 / 列表 / 单号 / 纯计数，required 为空）：把召回到的一律清掉，生成 SQL 不参考指标；
    - 全对上了：只留 indexes 那几条（可能一条，也可能多条），交给补充信息那一步；
    - 有对不上的（口径冲突 / 没召回 / 只有近似项）：把反问写进 state.clarification、转去
      ask_metric_definition——那里用 interrupt 停下来问用户口径，拿到回答后核对并落库（见 save_metrics），
      补齐的指标随状态继续往下走；已经对上的那几条同时带上，不因缺一条就把现有口径丢掉。
    模型调用失败只记日志、照常往下走：这是防错的附加关卡，不该因偶发调用失败把整轮检索挡住。
    评估模式跳过本节点（见 Retriever.eval）：评估是批量跑的，没人来回答反问。

    本节点（与 ask_metric_definition 的几条正常出口）都是双路派发：add_extra_info 与 recognise_relations
    并行跑——前者补库与日期信息、后面接着生成 SQL，后者把表间关系落库、写完就结束（见 graph.build）。
    """
    if state.mode == Mode.EVALUATION:
        logger.info("validate_metrics: evaluation mode, skip")
        return Command(goto=["add_extra_info", "recognise_relations"])

    writer = runtime.stream_writer
    writer(Step(message="正在检查指标口径", status=StepStatus.RUNNING))
    metric_infos = list(state.metric_infos or [])
    logger.info(f"validate_metrics: question={state.question}, {len(metric_infos)} metric(s)")

    payload = {
        "question": state.question,
        "metrics": [x.dump() for x in metric_infos],
    }
    match, error = await invoke(runtime.context.model, [
        SystemMessage(content=METRIC_MATCHING_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], MetricMatch)
    if error:
        logger.warning(f"validate_metrics: {error}")
        writer(Step(message="指标检查失败，已跳过", status=StepStatus.SUCCESS))
        return Command(goto=["add_extra_info", "recognise_relations"])

    # 用户输入涉及的指标，以及其中没有候选项对得上的那些（模型按用户原话给出）
    required = [x for x in match.required or [] if x]
    missing = [x for x in match.missing or [] if x]
    # 按下标取回保留的指标：越界 / 重复的下标（模型编的）直接丢掉，顺序保持模型的判断（相关度从高到低）
    kept = [metric_infos[index] for index in dict.fromkeys(match.indexes or [])
            if 0 <= index < len(metric_infos)]

    if not required:
        # 问题本身不需要指标：召回的一律清掉，生成 SQL 时不再被无关指标带偏
        logger.info("validate_metrics: metrics not needed, drop all")
        writer(Step(message="指标检查通过", status=StepStatus.SUCCESS))
        return Command(goto=["add_extra_info", "recognise_relations"], update={"metric_infos": []})

    if not missing and kept:
        logger.info(f"validate_metrics: kept {len(kept)} of {len(metric_infos)} metric(s), reason={match.reason}")
        writer(Step(message="指标检查通过", status=StepStatus.SUCCESS))
        return Command(goto=["add_extra_info", "recognise_relations"], update={"metric_infos": kept})

    # 有对不上的：模型没给 missing（如给的下标全越界）时按 required 全缺处理，免得带着缺口径的指标往下走
    missing = missing or required
    logger.info(f"validate_metrics: missing {missing}, kept {len(kept)}, reason={match.reason}")
    clarification = match.clarification or f"请补充说明{'、'.join(missing)}的统计口径（统计范围、过滤条件、时间口径）。"
    writer(Step(message="需要补充指标定义", status=StepStatus.ERROR, clarification=clarification))
    # 对上的那几条一起带上：问的是缺的口径，已有的口径不必丢（补齐的指标由 ask_metric_definition 追加）
    return Command(goto="ask_metric_definition",
                   update={"metric_infos": kept, "clarification": clarification})


async def save_metrics(validation: MetricAnswer, database_id: int,
                       embedding_model: Embeddings) -> list[MetricInfo]:
    """把用户确认过的指标口径落库：逐条写元数据库，再一次性向量化写进该库的指标集合（见 Qdrant.add_metrics）。

    :param validation: 审核结果，用的就是它整理出的 metrics（一条回答可能补齐好几个指标的口径，逐条都会存）
    :param database_id: 指标归属的业务库编号：指标按库划分，只在所属库里召回（见 models.meta.Metric.database）
    :param embedding_model: 嵌入模型（图上下文的 Context.embedding_model），与检索侧同源
    :return: 落库后的指标信息列表（编号即元数据库里那几条的编号）；没整理出带名称的指标时为空列表

    每条都新登记、**不查重**：即使库里已有同名指标也照写——同名的两条可能口径不同（含不含退款、
    按下单还是按支付时间），合并反而把口径搅在一起；同一个库里允许多条同名指标，召回时按名称 + 描述区分。
    向量库是派生数据：向量化失败只记日志，指标本身已经登记好了，重跑一次向量化（见 Qdrant.update_metrics）
    即可补齐，不影响这一轮继续生成 SQL。
    """
    saved = []
    for definition in validation.metrics or []:
        name = (definition.name or "").strip()
        if not name:
            # 名称是指标在指标库里的凭据（检索也按它拼文本），这一条没给名称就没法登记，跳过即可
            logger.warning(f"save_metrics: skip a metric without name: {definition}")
            continue
        metric = await Metric.create(name=name,
                                     description=(definition.description or "").strip() or None,
                                     alias=[x for x in (definition.alias or []) if x],
                                     database_id=database_id)
        logger.info(f"save_metrics: created metric {metric.id} ({name}) in database {database_id}")
        saved.append(metric)

    if not saved:
        return []

    try:
        await qdrant.add_metrics(database_id, saved, embedding_model)
    except Exception as e:  # noqa: BLE001 - 向量库是派生数据，失败只影响召回，不影响本轮查询
        logger.warning(f"save_metrics: 写入向量库失败（{len(saved)} 条）：{e}")

    return [MetricInfo(id=metric.id, name=metric.name or "", description=metric.description or "",
                       alias=metric.alias or []) for metric in saved]


async def ask_metric_definition(state: State, runtime: Runtime[Context]):
    """问用户指标口径，并当场审核他的回答：能用就继续，不行就换个问法再问，跳回本节点实现多轮。

    :param state: 图状态，clarification 是要问用户的那句话（由 validate_metrics 写入）；
        retry_count 记已经重问过几次（与 SQL 纠错共用同一个字段，确认后清零）
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model

    「问」与「审」合在一个节点里，靠「跳回自己」把多轮串起来：

    1. 一进节点就 interrupt，把 clarification 那句话交给客户端，这一轮停在这里；
    2. 客户端带同一个 thread_id、把用户的回答作为 content 再 POST 一次，图从本节点重新执行，interrupt
       直接返回那句回答（不再暂停）；
    3. 回答过一遍审核（提示词见 prompts.metric_answer_validation.md）——"呃呃啊啊"这类敷衍、答非所问、
       与指标明显不符都不采纳；能用就把整理出的指标逐条落库并向量化（见 save_metrics——一句回答可能补齐
       好几个指标的口径，一条也不许漏），再带着这些指标双路派发：补充信息与关联识别并行跑（见
       validate_metrics 文档），顺手清掉 clarification、把 retry_count 归零（后面 SQL 纠错还要用这个计数，
       见 validate_sql）；
    4. 不能用就换一种问法写回 clarification，再 Command 回本节点：节点重新执行时会再次 interrupt，于是
       又停住问一次。重问超过 MAX_ASK_RETRY 次还答不到点子上，就以「指标口径未确认」结束这一轮，不再纠缠。

    审核调用失败按「已补充」放行：这是防错的附加关卡，不该因偶发调用失败把整轮检索卡在反问里。
    评估模式不反问（见 Retriever.eval），直接放行。
    """
    if state.mode == Mode.EVALUATION:
        logger.info("ask_metric_definition: evaluation mode, skip")
        return Command(goto=["add_extra_info", "recognise_relations"])

    writer = runtime.stream_writer
    question = state.clarification or "请补充说明这个指标的统计口径（统计范围、过滤条件、时间口径）。"
    logger.info(f"ask_metric_definition: {question} (retry {state.retry_count})")
    # 停在这里等用户回答：第一次跑到这儿抛 GraphInterrupt；恢复时直接返回用户的回答
    answer = str(interrupt({"question": question,
                            "metrics": [x.dump() for x in state.metric_infos or []]}) or "").strip()
    logger.info(f"ask_metric_definition: answer={answer!r}")

    writer(Step(message="正在核对指标定义", status=StepStatus.RUNNING))
    if answer:
        payload = {
            "question": state.question,
            "metrics": [x.dump() for x in state.metric_infos or []],
            "asked": question,
            "answer": answer,
        }
        validation, error = await invoke(runtime.context.model, [
            SystemMessage(content=METRIC_ANSWER_PROMPT),
            HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
        ], MetricAnswer)
    else:
        # 恢复时内容是空的（客户端给了空串）：当作没回答，走下面「再问一次」的分支
        validation, error = None, None

    if error:
        logger.warning(f"ask_metric_definition: {error}")
        writer(Step(message="指标定义核对失败，按已补充处理", status=StepStatus.SUCCESS))
        return Command(goto=["add_extra_info", "recognise_relations"], update={"clarification": None, "retry_count": 0})

    if validation is not None and validation.accepted:
        logger.info(f"ask_metric_definition: accepted, definition={answer!r}")
        # 口径确认了：逐条落库并向量化到本库的指标集合，下次同类问题直接召回（见 save_metrics；
        # 一句回答可能补齐好几个指标）
        metric_infos = await save_metrics(validation, state.database_id, runtime.context.embedding_model)
        update: dict[str, Any] = {"clarification": None, "metric_definition": answer, "retry_count": 0}
        if metric_infos:
            # 把刚登记的这些也放进候选指标：后面生成 SQL 要按它们写口径（同名的照样并列，不覆盖召回的那些）
            update["metric_infos"] = [*(state.metric_infos or []), *metric_infos]
        writer(Step(message="指标定义已确认", status=StepStatus.SUCCESS))
        return Command(goto=["add_extra_info", "recognise_relations"], update=update)

    # 回答不可用（空回答或不采纳）：还能再问就换个问法跳回本节点，问够了就按失败结束
    reason = "用户没有给出回答" if not answer else "回答不可用"
    clarification = question
    if validation is not None:
        reason = validation.reason or reason
        clarification = validation.clarification or question
    if state.retry_count >= MAX_ASK_RETRY:
        error = f"指标口径未确认（已重问 {state.retry_count} 次）：{reason}"
        logger.warning(f"ask_metric_definition: {error}")
        writer(Step(message="指标口径未确认", status=StepStatus.ERROR, error=error, clarification=question))
        return Command(goto=END, update={"error": error})

    logger.info(f"ask_metric_definition: rejected ({reason}), ask again")
    writer(Step(message="需要补充指标定义", status=StepStatus.ERROR, clarification=clarification))
    # 回本节点：重新执行时会再次 interrupt，把新问法交给用户；retry_count 记一次重问
    return Command(goto="ask_metric_definition",
                   update={"clarification": clarification, "retry_count": state.retry_count + 1})


async def rerank_tables(state: State, runtime: Runtime[Context]):
    """重排候选表：按问题把召回的表压到最相关的一批（断崖截断，见 pop_top_k）。

    :param state: 图状态，table_infos 是 recall_columns 组装好的候选表（含字段与取值）
    :param runtime: 图运行上下文，重排模型取自 runtime.context.rerank_model

    排在 recall_columns 之后、filter_tables 之前，与指标那一路（recall_metrics → rerank_metrics）并行跑：
    这里只管表；表是生成 SQL 的必要条件，没有候选表直接以失败结束。查询文本是「关键词 + 用户问题」的拼接
    （见 build_rerank_query）：关键词补上问题里可能被切碎的业务词汇，原句给出完整语义。重排文本见
    build_table_text（表名 + 描述 + 字段，让重排模型看到表承载的语义），指令见 TABLE_RERANK_INSTRUCT。

    这里只产出本轮的 ``table_infos``（重排后的顺序），不碰 ``final_table_infos``：跨轮累加放在细筛之后
    （见 filter_tables），只有真正被选中的表才进累加列表，候选里被淘汰的不留。此后读表的是细筛与它下游的
    节点，用的都是 ``final_table_infos``。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在重排候选表", status=StepStatus.RUNNING))

    # table_infos 是列表，下标即重排请求里的文档下标
    table_infos = list(state.table_infos or [])
    query = build_rerank_query(state.keywords, state.question)
    logger.info(f"rerank: query={query!r}, {len(table_infos)} table(s)")

    # 表是生成 SQL 的必要条件，没有候选表则无需重排，直接结束
    if not table_infos:
        logger.warning("rerank: no candidate table to rerank")
        writer(Step(message="重排失败", status=StepStatus.ERROR, error="没有候选表，无法生成 SQL"))
        return Command(goto=END, update={"error": "没有候选表，无法生成 SQL"})

    try:
        results = await rerank_client.rerank(query, [build_table_text(x) for x in table_infos],
                                             runtime.context.rerank_model, instruct=TABLE_RERANK_INSTRUCT)
    except Exception as e:
        logger.error(e)
        writer(Step(message="重排失败", status=StepStatus.ERROR, error=str(e)))
        return Command(goto=END, update={"error": str(e)})

    # 重排结果已按相关度降序，断崖式截断后按原顺序取回（列表顺序即重排后的顺序；同一下标给两次只留一份）
    new_table_infos = list({table_infos[x.index].id: table_infos[x.index] for x in decide_top_k(results)}.values())
    logger.info(f"rerank: kept {len(new_table_infos)} of {len(table_infos)} table(s)")
    writer(Step(message="重排完成", status=StepStatus.SUCCESS))
    # 只交本轮结果：跨轮累加放在细筛之后（见 filter_tables），淘汰掉的候选不进累加列表
    return Command(goto="filter_tables", update={"table_infos": new_table_infos})


async def recall_again(state: State, runtime: Runtime[Context], error: str,
                       table_infos: list[TableInfo]) -> Command:
    """没有可用的表时先补召回：产出新一轮的问题与关键词跳回召回，实在补不了才按失败结束。

    :param state: 图状态（用原始问题判断该补什么）
    :param runtime: 图运行上下文
    :param error: 补召回也没戏时的失败原因（没有表就生成不出 SQL）
    :param table_infos: 还没筛出结果的表（可能为空），交给模型判断缺什么
    :return: 跳回 recall_columns 的 Command；次数用完或模型给不出召回输入时，是带 error 的尾节点跳转

    由 :func:`filter_tables` 的两处「一张表都没有」调用（进节点时就没有候选表、细筛后一张不剩）：这两种
    情况都不是「问题无解」——多半是关键词没召回全，故先走一轮补召回（见 :func:`probe_recall`），次数上限
    与生成 SQL 前的检查同一个（:data:`MAX_RECALL_RETRY`）；补召回本身没得用（模型调用失败、判为够用却
    一张表都没有、次数用完）才收场。
    """
    if state.probe_count < MAX_RECALL_RETRY:
        command = await probe_recall(state, runtime, table_infos)
        if command is not None:
            return command
    writer = runtime.stream_writer
    logger.warning(f"filter_tables: {error}")
    writer(Step(message="数据表过滤失败", status=StepStatus.ERROR, error=error))
    return Command(goto=END, update={"error": error})


async def filter_tables(state: State, runtime: Runtime[Context]):
    """在重排之后按问题细筛表：只留下回答问题真正需要的那几张，再把选中的表并入累加列表。

    :param state: 图状态，table_infos 是本轮重排后的候选表（列表，顺序即相关度顺序）
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model；嵌入与重排模型这里用不到

    重排按向量相关度截过一轮，这里再用一次语义细筛（提示词见 prompts.table_filtering.md）：模型只输出
    表编号，编号回 state.table_infos 取表——模型编出来的编号（不在候选里）直接丢掉，表内容也轮不到
    模型复述。候选表本来就带着字段与取值，payload 取 TableInfo.dump()（字段上不带 table_id），
    与提示词里的输入格式一一对应。
    没有表时先**补召回**：进本节点时就没有候选表（正常走不到，重排那一步已经拦过一次）、细筛后一张不剩，
    都先交给模型产出一轮新的问题与关键词、跳回 recall_columns 再召回一次（见 recall_again / probe_recall，
    次数上限 MAX_RECALL_RETRY）；补召回也补不出来才按失败结束——没有表就生成不出 SQL。

    筛完之后做两件事：
    1. 把选中表的取值**补全**：候选表阶段只带了元数据库里的取值样本，这里再从向量库召回这些表里同步字段
       （synchronized=True）的取值（见 load_synchronized_values）、与样本合并去重后补进对应字段，
       下游生成 SQL 看到的就是「该列在数据库中的取值」；
    2. 把选中的表并进 ``final_table_infos``（见 merge_table_infos）：本轮选中的在前，此前各轮选中、本轮
       没选上的接在后面——补召回会重新召回（见 probe_recall），新关键词未必还能命中上一轮找到的
       表，累加着才不会丢；此后的节点（生成 SQL、识别关系）都只认这一份。
    整表字段在召回阶段已经取全（见 recall_columns / load_table_columns），这里不再补字段。

    本节点是字段这一路的收口，出口分两种：
    - 首轮（probe_count 为 0）：只写 final_table_infos、不自己决定下一步，validate_metrics 由 graph.build 里的 join
      边触发（等指标那一路的 rerank_metrics 也跑完，两路才汇合，口径核对只做一次）；
    - 补召回后的重跑（probe_count > 0，见 probe_recall）：指标口径首轮已经核对过、指标那一路也没重跑，
      join 永远等不到另一侧，故直接 Command 进汇合之后的双路派发（补库与日期 + 关系识别）。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在过滤数据表", status=StepStatus.RUNNING))
    table_infos = list(state.table_infos or [])
    # 重排阶段已保证有候选表（为空时重排就以错误结束），这里为空只可能是调用方直接跳进来
    if not table_infos:
        # 一张表都没有不代表问题无解：先补召回一轮（join 边这一侧不写，validate_metrics 也就不会被触发）
        return await recall_again(state, runtime, "没有可用的表，无法生成 SQL", [])

    payload = {
        "question": state.question,
        "tables": [x.dump() for x in table_infos],
    }
    table_ids, error = await invoke(runtime.context.model, [
        SystemMessage(content=TABLE_FILTERING_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], TableIdList)
    if error:
        logger.error(error)
        writer(Step(message="数据表过滤失败", status=StepStatus.ERROR, error=error))
        return Command(goto=END, update={"error": error})

    # 按模型给出的顺序回候选表取表（那个顺序就是它与问题的相关程度，见提示词），不在候选里的编号略过
    table_of = {table.id: table for table in table_infos}
    filtered = [table_of[table_id] for table_id in table_ids if table_id in table_of]
    logger.info(f"filter_tables: kept {len(filtered)} of {len(table_infos)} table(s) -> "
                f"{[table.id for table in filtered]}")
    if not filtered:
        # 细筛把候选全筛掉了：同样先补召回一轮，看换个问题与关键词能不能召回对
        return await recall_again(state, runtime, "过滤后没有可用的表，无法生成 SQL", table_infos)

    # 筛完之后把选中表的取值补全：候选表阶段只带了元数据库里的取值样本，这里再从向量库召回这些表里
    # 同步字段（synchronized=True）的取值，与样本合并去重后补进对应字段
    extra = await load_synchronized_values([table.id for table in filtered], state.database_id)
    filtered = [replace(table, columns=[
        replace(column, values=merge_values(column.values, extra.get(column.id, [])))
        for column in (table.columns or [])])
                for table in filtered]
    logger.info(f"filter_tables: merged synchronized value(s) of {len(extra)} column(s) into "
                f"{len(filtered)} table(s)")
    # 细筛完成才合并：本轮选中的在前，此前各轮选中、本轮没选上的接在后面（见 merge_table_infos）
    final_table_infos = merge_table_infos(state.final_table_infos, filtered)
    logger.info(f"filter_tables: final_table_infos -> {len(final_table_infos)} table(s) "
                f"(kept {len(filtered)} this round)")
    writer(Step(message="数据表过滤完成", status=StepStatus.SUCCESS))
    if state.probe_count:
        # 补召回后的重跑（见 probe_recall）：指标那一轮已经核对过口径、本轮也没重跑指标那一路，
        # 不能再等 join（它会一直等 rerank_metrics），直接进汇合之后的双路派发
        return Command(goto=["add_extra_info", "recognise_relations"],
                       update={"final_table_infos": final_table_infos})
    # 首轮：不走 Command，validate_metrics 由 join 边触发，要等指标那一路重排完（见 graph.build 的 add_edge）
    return {"final_table_infos": final_table_infos}


async def recognise_relations(state: State, runtime: Runtime[Context]):
    """识别表间关联并落库：把「哪两个字段能 JOIN」写进 ColumnRelation 中间表。

    :param state: 图状态，final_table_infos 是过滤后的表（列表，含字段）
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model

    排在 validate_metrics（或问完口径的 ask_metric_definition）之后，与 add_extra_info 并行跑（见
    graph.build）：只就这批真正用到的表问一次模型（提示词见 prompts.relation_recognition.md），识别结果
    最贴近查询。

    落库存的是字段编号（Column.id → Column.related_columns 这个多对多）：一条关系写正反两行，
    两端互相登记，从哪边查 JOIN 都查得到；`<表名>.<字段名>` 是读取时拼的（见 build_column_info），
    不落库——表名与字段名改起来还牵动拼写，编号才是稳定标识。中间表 (source, target) 唯一，
    重复识别（模型把同一条给出多次、甚至正反各给一次）由 ignore_conflicts 跳过，不会插出重复行。
    已有关系只增不删：关系是库结构的既有事实，别处采集不会让它们失效；字段删掉时中间表随外键级联清掉。
    例外是字段语义变了——描述或别名被改（见 controller 的 update_column）时，挂在该
    字段上的关系会被清掉，之后由本节点按新描述重新识别。

    关系是附加产物、不影响本次生成 SQL：模型调用失败、或一条都没识别出来时只记日志、照常走完，
    不像其他节点那样以错误帧结束整轮检索。写完就跳到 END——本轮生成 SQL 用的是召回时已登记的关系
    （见 recall_columns 的 load_related_refs），这里新识别出来的留给下一次，不必挡在生成 SQL 前面。
    """
    writer = runtime.stream_writer
    writer(Step(message="正在识别表间关系", status=StepStatus.RUNNING))
    table_infos = list(state.final_table_infos or [])
    # 至少两张表才谈得上跨表关联：一张表（或没有表）直接跳过，省一次模型调用
    if len(table_infos) < 2:
        logger.info(f"recognise_relations: {len(table_infos)} table(s), skip")
        writer(Step(message="表间关系识别完成", status=StepStatus.SUCCESS))
        return Command(goto=END)

    payload = {"tables": [x.dump() for x in table_infos]}
    relations, error = await invoke(runtime.context.model, [
        SystemMessage(content=RELATION_RECOGNITION_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], RelationList)
    if error:
        logger.warning(f"recognise_relations: {error}")
        writer(Step(message="表间关系识别失败，已跳过", status=StepStatus.SUCCESS))
        return Command(goto=END)

    # (表编号, 字段名) → 字段编号：两端只认这批表里真实存在的字段，模型编出来的表或字段直接丢掉
    column_of: dict[tuple[str, str], str] = {}
    for info in table_infos:
        for column in info.columns or []:
            if column.name:
                column_of[(info.id, column.name)] = column.id

    # 关系是无向的：按字段编号排好序去重——模型把同一条给出多次、甚至正反各给一次，都只算一条
    pairs: set[tuple[str, str]] = set()
    for relation in relations:
        source_id = column_of.get((relation.source_table, relation.source_column))
        target_id = column_of.get((relation.target_table, relation.target_column))
        if not source_id or not target_id or source_id == target_id:
            logger.warning(f"recognise_relations: skipped {relation}")
            continue
        pairs.add((source_id, target_id) if source_id < target_id else (target_id, source_id))

    if not pairs:
        logger.info(f"recognise_relations: no relation discovered among {len(table_infos)} table(s)")
        writer(Step(message="表间关系识别完成", status=StepStatus.SUCCESS))
        return Command(goto=END)

    # 一条关系写两行：正反各登记一次，从哪边查 JOIN 都查得到。中间表 (source, target) 唯一，
    # 重复识别由 ignore_conflicts 跳过，不会插出重复行
    rows = []
    for source_id, target_id in pairs:
        rows.append(ColumnRelation(source_id=source_id, target_id=target_id))
        rows.append(ColumnRelation(source_id=target_id, target_id=source_id))
    await ColumnRelation.bulk_create(rows, ignore_conflicts=True)
    logger.info(f"recognise_relations: {len(pairs)} relation(s) discovered "
                f"({len(relations)} returned by model), {len(rows)} row(s) written")
    writer(Step(message="表间关系识别完成", status=StepStatus.SUCCESS))
    return Command(goto=END)


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


# 补召回最多几次：补一次通常就能把漏掉的表捞回来，连着几轮模型还判「不够用」就不再折腾
MAX_RECALL_RETRY = 3


def sql_question(state: State) -> str:
    """生成（与校正）SQL 时用哪个问题：补召回跑过就用原始问题，否则用当前问题（可能是改写后的）。

    补召回会把 state.question 换成「换个说法」的那一版（见 probe_recall），那是给召回用的；拿来生成 SQL
    会让 SQL 跟着临时表述跑偏——用户真正要什么，以 original_question 为准。
    """
    if state.probe_question:
        return state.original_question or state.question
    return state.question


async def probe_recall(state: State, runtime: Runtime[Context],
                       table_infos: list[TableInfo]) -> Command | None:
    """让模型判断现有表 / 字段够不够回答问题；不够就产出新一轮的召回输入，返回跳回召回的 Command。

    :param state: 图状态（用原始问题作为「用户到底要什么」的判据）
    :param runtime: 图运行上下文，LLM 取自 runtime.context.model
    :param table_infos: 当前选中的表：交给模型判断「已经有什么」、还缺什么
    :return: 跳回 recall_columns 的 Command（带上新的问题与关键词）；模型判为够用、调用失败、或判为
        不够用却给不出可用的召回输入时返回 None（调用方自行决定收场：生成 SQL 前那一处照常生成，
        细筛处那一处按失败结束，见 recall_again）

    判断完全交给模型（提示词见 prompts/recall_probe.md）：够用的口径是「要分组的字段、要统计的度量、
    要筛选的条件、跨表的 JOIN 键」都能在候选表里找到落点；只看关键词有没有字面命中太粗（同义说法、
    字段改个名都会误判），故不做本地匹配。

    调用方两处：生成 SQL 之前查一次召回够不够（见 generate_sql）；细筛时一张表都没有也先来这里产出一轮
    新的问题与关键词再召回（见 filter_tables / recall_again）——后一种情况下 table_infos 可能为空，
    模型只能判「不够用」，照常给出问题与关键词即可。

    产出的一对问题与关键词同时写进 state.probe_question / probe_keywords（标记这是补召回的一轮），
    并替换 state.question / state.keywords 供召回与重排使用。新问题**只服务于召回**，可以与原问题完全不同：
    原问题问「员工的姓名和工号」、候选里缺部门表时，模型可能只给「有哪些部门？」——这正是想要的，越贴近
    缺失对象召回越准。生成 SQL 时仍回到原始问题（见 sql_question），SQL 的语义不会被这一版带偏。
    """
    writer = runtime.stream_writer
    # 提交完整的表信息（TableInfo.dump：表的编号 / 名称 / 类型 / 描述 + 每个字段的编号、名称、类型、描述、
    # 别名、取值、角色与可关联字段）——判断「缺不缺」要看取值形态与角色，只给「字段名（类型）：描述」不够；
    # 与细筛、生成 SQL 用的是同一份结构（见 prompts 里各处的输入格式）
    payload = {
        "question": state.original_question or state.question,
        "tables": [table.dump() for table in table_infos],
    }
    probe, error = await invoke(runtime.context.model, [
        SystemMessage(content=RECALL_PROBE_PROMPT),
        HumanMessage(content=f"```json\n{dump(payload, default=str)}\n```")
    ], RecallProbe)
    if error:
        logger.warning(f"probe_recall: {error}，照常生成 SQL")
        return None
    if probe.sufficient:
        logger.info(f"probe_recall: 现有表与字段够用，直接生成 SQL；reason={probe.reason}")
        return None

    question = (probe.question or "").strip()
    keywords = [keyword for keyword in probe.keywords or [] if keyword]
    if not question and not keywords:
        logger.info("probe_recall: 判为不够用但没给出可用的召回输入，照常生成 SQL")
        return None

    new_question = question or state.question
    new_keywords = keywords or list(state.keywords or [])
    logger.info(f"probe_recall: 判为不够用 -> question={new_question!r}，"
                f"{len(new_keywords)} keyword(s)：{new_keywords}；"
                f"missing_tables={probe.missing_tables}，missing_columns={probe.missing_columns}；"
                f"reason={probe.reason}")
    writer(Step(message="发现缺表或缺字段，重新召回", status=StepStatus.SUCCESS))
    return Command(goto="recall_columns", update={
        "question": new_question,
        "keywords": new_keywords,
        "probe_question": new_question,
        "probe_keywords": new_keywords,
        "probe_count": state.probe_count + 1,
        # 新一轮召回会重新生成 SQL，纠错预算从零开始算（见 validate_sql）
        "retry_count": 0,
    })


async def generate_sql(state: State, runtime: Runtime[Context]):
    writer = runtime.stream_writer
    writer(Step(message="正在生成 SQL", status=StepStatus.RUNNING))

    table_infos = list(state.final_table_infos or [])
    # 表是生成 SQL 的必要条件，没有表则无从生成，直接结束
    if not table_infos:
        logger.warning("generate_sql: no candidate table in state")
        writer(Step(message="SQL 生成失败", status=StepStatus.ERROR, error="没有候选表，无法生成 SQL"))
        return Command(goto=END, update={"error": "没有候选表，无法生成 SQL"})

    # 生成之前先让模型判断一次召回够不够（见 probe_recall）：判为不够用就换个问题与关键词回去再召回，
    # 补召回之后回到这里再判一次，直到够用、或者次数用完（见 MAX_RECALL_RETRY）
    if state.probe_count < MAX_RECALL_RETRY:
        command = await probe_recall(state, runtime, table_infos)
        if command is not None:
            return command
    else:
        logger.warning(f"generate_sql: 已补召回 {state.probe_count} 次，用现有表生成 SQL")

    # 补召回跑过就用原始问题生成 SQL（见 sql_question），别按临时改写的问题写
    payload = {
        "question": sql_question(state),
        "tables": [x.dump() for x in table_infos],
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
            # 评估模式通过 EXPLAIN 校验后也往下执行：这一帧先交出校验无误的 SQL 与本次检索到的表信息，
            # 执行完那一帧还会再交一次（评估侧按「SQL 与表片段同帧」取结果，见 graph.Retriever.eval 与
            # models.dataset.Result.retrieved_contexts）；执行也能顺带确认 SQL 真能跑通
            logger.info("validate_sql: evaluation mode, execute to verify the SQL runs")
            writer(Step(
                message="SQL 校验通过",
                status=StepStatus.SUCCESS,
                sql=sql,
                table_infos=list(state.final_table_infos or [])
            ))
            return Command(goto="execute_sql", update={"error": None})
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
        # 与生成 SQL 同一口径：补召回跑过就用原始问题校正（见 sql_question）
        "question": sql_question(state),
        "sql": state.sql,
        "error": state.error,
        "tables": [x.dump() for x in state.final_table_infos or []],
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
                table_infos=list(state.final_table_infos or [])))
    # 结果已经随上面的帧推给前端：本轮到此结束（数据探测节点已删，终点见 graph.build）
    return Command(goto=END)
