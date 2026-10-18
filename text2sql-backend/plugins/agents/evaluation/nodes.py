from asyncio import Semaphore, as_completed, create_task, gather
from dataclasses import asdict
from datetime import datetime

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph.graph import END
from tortoise.transactions import in_transaction

from models import explain_sql, get_column_values
from models.dataset import Dataset, DatasetItem, Result, ResultItem
from models.meta import Column, Table
from plugins.agents.models import invoke
from plugins.agents.evaluation.prompts import EVALUATION_REVIEW_PROMPT, QUESTION_GENERATION_PROMPT, \
    SQL_CORRECTION_PROMPT, SQL_GENERATION_PROMPT
from plugins.agents.evaluation.schemas import ColumnInfo, Context, DatasetItemInfo, EvaluationReview, SqlList, \
    State, Step, StepStatus, StringList, TableInfo
from plugins.json import dump

# 每个字段取回的前若干条取值，供模型参考
VALUES_LIMIT = 5
# SQL 校验的批大小：按该数量为单位用 EXPLAIN 校验
SQL_BATCH_SIZE = 5
# 评测阶段的并发度：每个问题都要跑一整条检索图、再做一次指标判定，都是大模型请求，
# 并发过高会触发服务端限流，故与采集图同样限制在个位数
EVALUATION_CONCURRENCY = 5
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：落库数据集（数据集与条目）在一个事务里完成
META_CONNECTION = "meta"


def database_id_of(state: State) -> int:
    """取本轮评测的业务库编号（见 schemas.State.database_id）。

    State.database_id 带默认值 None：把编号挪进状态之前建的会话（checkpoint 里没有这个通道）遇到必填
    字段会连状态都装配不起来，留个默认值才让它们还能打开；恢复这类会话时由 Evaluator.invoke 用本次入参
    把编号补写进状态，故正常跑起来时它一定有值。真取到 None（有人绕开 invoke 直接跑图）时这里直接报错，
    别让它一路走到「连接名为 database.None」那种看不懂的报错。
    """
    if state.database_id is None:
        raise ValueError("会话状态里没有业务库编号：请走 Evaluator.invoke（它会用本次入参补上）")
    return state.database_id


async def build_table_infos(table_ids: list[str], database_id: int) -> list[TableInfo]:
    """按表编号取回库上下文：表描述、字段描述 / 别名 / 角色，以及每个字段的前 VALUES_LIMIT 条取值。

    :param table_ids: 出题所用的表编号（元数据库 Table.id，形如 <库编号>.<表名>，见
        schemas.State.table_ids）
    :param database_id: 本次所用业务库编号（Database.id）：字段取值在该库上取（见 models.get_connection）
    :return: 表信息快照，顺序与传入的编号一致；元数据库里已找不到的表自动略过

    出题节点用它组装出题所用的表，评测节点在开跑前用它重新取一遍（同一次运行里只有这两个节点取表：
    纠正节点直接沿用出题节点组装的那份，见 correct_sql）。
    """
    ids = list(dict.fromkeys(table_ids))
    tables = await Table.filter(id__in=ids).all()
    if not tables:
        logger.warning(f"build_table_infos: none of {len(ids)} table id(s) exists")
        return []
    columns = await Column.filter(table_id__in=[x.id for x in tables]).all()
    grouped = {}
    for column in columns:
        grouped.setdefault(column.table_id, []).append(column)
    # column.table_id 是表编号，未必等于业务表名，取值查询需按真实表名
    table_names = {table.id: table.name for table in tables}
    # 同一次查询取回每个字段的前 VALUES_LIMIT 条取值，按「表名.列名」归组（供模型理解字段的业务含义）
    values = await get_column_values(
        [(table_names[column.table_id], column.name, VALUES_LIMIT, 0) for column in columns], database_id
    )
    infos = {
        table.id: TableInfo(
            id=table.id,
            name=table.name,
            role=table.role,
            description=table.description,
            columns=[
                ColumnInfo(
                    id=column.id,
                    name=column.name,
                    type=column.type,
                    description=column.description,
                    alias=column.alias,
                    role=column.role,
                    values=values.get(f"{table_names[column.table_id]}.{column.name}", []),
                )
                for column in grouped.get(table.id, [])
            ],
        )
        for table in tables
    }
    logger.info(f"build_table_infos: built {len(infos)} of {len(ids)} table(s)")
    # 按传入顺序归位，保持上下文顺序稳定（表已被删掉的自然从上下文里去掉）
    return [infos[table_id] for table_id in ids if table_id in infos]


async def generate_questions(state: State, runtime: Runtime[Context]) -> Command:
    """基于状态里带进来的表编号（外部指定）及其最新表信息，生成 State.size 个问题与对应的 SQL。

    :param state: 图状态，size 指定生成的问题数量，table_ids 为出题所用的表编号（外部指定，见
        graph.Evaluator.invoke 的 table_ids 入参）；
        组装出的表信息随更新写回状态（state.table_infos），供纠正在不刷新的情况下直接沿用
    :param runtime: 图运行上下文，LLM 实例取自 runtime.context.model
    :return: 更新 state.dataset_info（问题与 SQL 配对）与 state.table_infos（出题所用的表信息）的指令；
        校验失败时跳到尾节点并写入 error

    表是出题的必要条件，没有表（或指定的表都已不存在）则直接结束；问题数量由 size 决定，先出题再基于问题
    与表生成 SQL。问题按字符串数组校验，SQL 按带整数 key 的 {key, sql} 列表校验，
    再按 key 与问题配对组装成 dataset_info。
    """
    writer = runtime.stream_writer
    model = runtime.context.model
    if not state.table_ids:
        logger.warning("generate_questions: no table in state")
        error = "没有可用的表，无法生成问题"
        writer(Step(message=error, status=StepStatus.FAILED, error=error))
        return Command(goto=END, update={"error": error})

    # 出题用的表信息在本节点组装：取的是元数据库当前的一份
    table_infos = await build_table_infos(state.table_ids, database_id_of(state))
    if not table_infos:
        logger.warning("generate_questions: none of the specified table exists")
        error = "指定的表已不存在，无法生成问题"
        writer(Step(message=error, status=StepStatus.FAILED, error=error))
        return Command(goto=END, update={"error": error})

    writer(Step(message=f"正在生成 {state.size} 个问题"))
    tables = [asdict(x) for x in table_infos]
    payload = {"n": state.size, "tables": tables}
    # 模型输出不一定是合法字符串数组，重试后仍不合规则终止
    questions, error = await invoke(model, [
        SystemMessage(content=QUESTION_GENERATION_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], StringList)
    if error:
        logger.error(error)
        writer(Step(message=f"问题生成失败：{error}", status=StepStatus.FAILED, error=error))
        return Command(goto=END, update={"error": error})
    logger.info(f"generate_questions: generated {len(questions)} question(s)")
    writer(Step(message=f"问题生成完成：共 {len(questions)} 个", status=StepStatus.SUCCESS))

    # 基于问题与表继续生成 SQL：为每个问题编号（key），结果按 key 对应返回
    writer(Step(message="正在生成 SQL"))
    payload = {
        "questions": [{"key": index, "question": question}
                      for index, question in enumerate(questions, start=1)],
        "tables": tables,
    }
    records, error = await invoke(model, [
        SystemMessage(content=SQL_GENERATION_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], SqlList)
    if error:
        logger.error(error)
        writer(Step(message=f"SQL 生成失败：{error}", status=StepStatus.FAILED, error=error))
        return Command(goto=END, update={"error": error})
    logger.info(f"generate_questions: generated {len(records)} SQL for {len(questions)} question(s)")
    writer(Step(message=f"SQL 生成完成：共 {len(records)} 条", status=StepStatus.SUCCESS))

    # 按 key 把 SQL 配回对应问题，组装成数据集条目
    sql_by_key = {record.key: record.sql for record in records}
    missing = [index for index in range(1, len(questions) + 1) if index not in sql_by_key]
    if missing:
        logger.warning(f"generate_questions: missing SQL for key(s): {missing}")
    dataset_info = [
        DatasetItemInfo(question=question, sql=sql_by_key[index])
        for index, question in enumerate(questions, start=1)
        if index in sql_by_key
    ]
    logger.info(f"generate_questions: assembled {len(dataset_info)} dataset item(s)")
    writer(Step(message=f"已组装 {len(dataset_info)} 条问题-SQL 对", status=StepStatus.SUCCESS))
    return Command(update={"dataset_info": dataset_info, "table_infos": table_infos})


async def correct_sql(state: State, runtime: Runtime[Context], config: RunnableConfig) -> Command:
    """以 SQL_BATCH_SIZE 条为单位用 EXPLAIN 校验 SQL，收集报错的 SQL 交给模型纠正。

    :param state: 图状态，dataset_info 为待校验的问题与 SQL，table_infos 为出题节点组装好的表信息
        （纠正不刷新，直接沿用这一份；它同时作为数据集的库上下文落库），database_id 为业务库编号
    :param runtime: 图运行上下文，LLM 实例取自 runtime.context.model
    :param config: 本次运行的配置（langgraph 注入）：会话编号在 config["configurable"]["thread_id"] 里，
        落库数据集时记它（见 models.dataset.Dataset.thread_id）
    :return: 更新 state.dataset_info（替换为纠正后的 SQL）与 state.dataset_id（落库的数据集编号）的指令；
        校验或纠正失败时跳到尾节点并写入 error

    1. 以 EXPLAIN 逐条校验 SQL，按 SQL_BATCH_SIZE 条一批批量下发，收集报错的 SQL 及其错误信息；
    2. 把报错的 SQL（连同问题、错误与可用表）一次性提交给模型纠正；
    3. 按 key 把纠正后的 SQL 写回对应条目，未报错的条目保持原样；
    4. 纠正完成后把数据集落库（见下方落库一段），连同会话编号一起存下，拿到数据集编号交给后续评测挂结果。
    """
    writer = runtime.stream_writer
    database_id = database_id_of(state)
    dataset_info = state.dataset_info or []
    if not dataset_info:
        logger.warning("correct_sql: empty dataset")
        writer(Step(message="数据集为空，无法校验 SQL", status=StepStatus.FAILED, error="数据集为空，无法校验 SQL"))
        return Command(goto=END, update={"error": "数据集为空，无法校验 SQL"})

    # 纠正提示词与数据集库上下文都靠这份表信息：沿用出题节点组装好的那份，不刷新——
    # 纠正紧跟出题，其间元数据没机会变，多取一次只是白跑一趟
    table_infos = state.table_infos or []

    writer(Step(message=f"正在校验 SQL（每 {SQL_BATCH_SIZE} 条一批）"))
    # 以 SQL_BATCH_SIZE 条为单位校验，收集报错的 SQL（key 为条目在数据集中的序号）
    items = []
    for start in range(0, len(dataset_info), SQL_BATCH_SIZE):
        batch = dataset_info[start:start + SQL_BATCH_SIZE]
        results = await gather(*(explain_sql(x.sql, database_id) for x in batch))
        for offset, (valid, error) in enumerate(results):
            if valid:
                continue
            items.append({
                "key": start + offset + 1,
                "question": batch[offset].question,
                "sql": batch[offset].sql,
                "error": error or "SQL is invalid",
            })
    logger.info(f"correct_sql: {len(items)} of {len(dataset_info)} SQL need correction")
    if items:
        writer(Step(message=f"收集到 {len(items)} 条报错的 SQL，正在纠正"))
        payload = {"items": items, "tables": [asdict(x) for x in table_infos]}
        records, error = await invoke(runtime.context.model, [
            SystemMessage(content=SQL_CORRECTION_PROMPT),
            HumanMessage(content=dump(payload, default=str)),
        ], SqlList)
        if error:
            logger.error(error)
            writer(Step(message=f"SQL 纠正失败：{error}", status=StepStatus.FAILED, error=error))
            return Command(goto=END, update={"error": error})

        # 按 key 把纠正后的 SQL 写回对应条目，其余条目维持原 SQL
        corrected = {record.key: record.sql for record in records}
        logger.info(f"correct_sql: corrected {len(corrected)} SQL")
        writer(Step(message=f"SQL 纠正完成：共纠正 {len(corrected)} 条", status=StepStatus.SUCCESS))
        dataset_info = [
            DatasetItemInfo(question=x.question, sql=corrected.get(index, x.sql))
            for index, x in enumerate(dataset_info, start=1)
        ]
    else:
        writer(Step(message=f"SQL 校验通过：{len(dataset_info)} 条均无需纠正", status=StepStatus.SUCCESS))

    # 纠正完成（含无需纠正）：把最终的问题与参考 SQL 落库，一条 Dataset 加逐条 DatasetItem。
    # reference_contexts 存下出题与判定所用的表结构（外部指定表编号、由本节点组装的
    # TableInfo）：判定两条 SQL 是否等效要看字段的业务含义，存下来既让数据集可复现，也让后续
    # 评测不必依赖当时的元数据状态（见 models.dataset.Dataset.reference_contexts）。thread_id 记下本轮会话，
    # 据此能把这批问题、以及之后在这个会话上跑的历次评测找回来（见 models.dataset.Dataset.thread_id）。
    # 数据集与条目在一个事务里写入，避免留下一条没有条目的数据集（那看起来与「空数据集」没区别）；
    # 名称按生成时间取，同一天存多份也互不覆盖。
    writer(Step(message="正在保存数据集"))
    # 会话编号取自运行配置（langgraph 把 thread_id 收成字符串，见 graph.Evaluator.invoke），Dataset.thread_id 是整数
    async with in_transaction(META_CONNECTION):
        dataset = await Dataset.create(name=f"数据集 {datetime.now():%Y-%m-%d %H:%M:%S}",
                                       thread_id=int(config["configurable"]["thread_id"]),
                                       reference_contexts=[asdict(x) for x in table_infos])
        await DatasetItem.bulk_create([
            DatasetItem(question=x.question, reference=x.sql, dataset_id=dataset.id)
            for x in dataset_info
        ])
    logger.info(f"correct_sql: saved dataset {dataset.id} with {len(dataset_info)} item(s)")
    # 落库后这一帧带上数据集编号：调用方据此对上数据集，不必等图跑完
    writer(Step(message=f"数据集已保存：共 {len(dataset_info)} 条", status=StepStatus.SUCCESS,
                dataset_id=dataset.id))
    return Command(update={"dataset_info": dataset_info, "dataset_id": dataset.id})


def render_schema(table_infos: list[TableInfo]) -> str:
    """把出题所用的表结构渲染成文本，作为 SQL 等效性判定的数据库上下文。

    :param table_infos: 出题与生成 SQL 时使用的表及其字段
    :return: 逐表逐字段的描述文本
    """
    lines = []
    for table in table_infos:
        lines.append(f"表 {table.name}（{table.role}）：{table.description or ''}".rstrip())
        for column in table.columns:
            description = f"{column.name} {column.type or ''}".strip()
            if column.description:
                description = f"{description}：{column.description}"
            lines.append(f"  - {description}")
    return "\n".join(lines)


async def evaluate(item: DatasetItemInfo,
                   context: Context,
                   database_id: int,
                   schema: str,
                   semaphore: Semaphore) -> tuple[str | None, float, str | None, list[dict]]:
    """单条评测：先跑检索得到待评 SQL，再与参考 SQL 比对打分。

    :param item: 待评测的条目（问题与参考 SQL）
    :param context: 图运行上下文：检索智能体取自 context.retriever、指标取自 context.metric
    :param database_id: 待评测的业务库编号（见 schemas.State.database_id）：检索智能体在该库上答题
    :param schema: 出题与判定所用的表结构文本，作为判定的数据库上下文交给指标
    :param semaphore: 并发额度，检索与指标调用共用：两者都是大模型请求，同时放开会触发服务端限流
    :return: (检索生成的 SQL, 等效性得分, 得分理由, 检索到的表信息)：SQL 未生成或没通过校验时为 None，得分为
        1.0（等效）/ 0.0（不等效或未生成 SQL），理由为指标的判定说明或失败原因；表信息是随着这条 SQL 一起
        检索到的表、字段与取值（逐条转成字典，供落库成 Result.retrieved_contexts），没有可用的 SQL 时为空列表

    检索没交出 SQL（没生成，或生成了但没通过校验）时不调用指标，直接记 0 分；单条失败（检索或指标抛异常）
    不向上抛，原因写进返回的理由，故一条失败不影响其余条目。
    """
    score, reason, response, tables_infos = 0.0, None, None, []
    try:
        async with semaphore:
            response, table_infos = await context.retriever.eval(item.question, database_id)
            tables_infos = [asdict(x) for x in table_infos]
            if not response:
                reason = "检索未生成 SQL"
            else:
                result = await context.metric.ascore(
                    response=response,
                    reference=item.sql,
                    reference_contexts=[schema]
                )
                score, reason = result.value, result.reason
    except Exception as e:
        logger.error(e)
        reason = str(e)
    return response, score, reason, tables_infos


async def evaluate_dataset(state: State, runtime: Runtime[Context]) -> Command:
    """用检索智能体跑一遍评测数据集，并以 SQL 语义等效性逐条打分。

    :param state: 图状态，dataset_id 为本轮落库的数据集编号（结果挂在它上面），dataset_info 为待评测的
        问题与参考 SQL，table_ids 为库上下文所用的表编号，database_id 为待评测的业务库编号
    :param runtime: 图运行上下文，检索智能体取自 runtime.context.retriever，指标取自 runtime.context.metric
    :return: 更新 state.table_infos（刷新后的表信息）的指令；没有可评测的数据集时跳到尾节点并写入 error

    评测前先按状态里的表编号重新取一遍表信息（表描述、字段描述 / 别名 / 角色、字段取值，见 build_table_infos）：
    出题到评测之间元数据可能被改过或重新采集过，而判定与解读报告都要靠这些信息，用最新的一份才判得准。

    1. 每条问题各起一个任务并发跑检索（走模块级的 evaluate：retriever.eval 带上业务库编号），
      得到待评 SQL 与检索到的表信息，并发度由 EVALUATION_CONCURRENCY 限制；
    2. 用指标（Ragas 的 SQL 语义等效性）把待评 SQL 与参考 SQL 比对，得 1.0（等效）/ 0.0（不等效）；
    3. 检索没产出 SQL 时不调用指标，直接记 0 分；单条失败（检索或指标抛异常）不影响其余条目，
       失败原因写进该条的理由；
    4. 结果按问题原顺序归位后，把表元信息与逐条结果（问题、参考 SQL、生成 SQL、得分与理由）交给模型，
       按 EVALUATION_REVIEW_PROMPT 写一份解读报告——整体结论、逐条归因、共性问题与改进建议，模型直接
       输出报告正文（纯文本，不做 JSON 包装）；报告生成失败只记日志与一条失败帧，不影响这一轮评测结果本身；
    5. 结果、解读报告与检索到的表信息一起落库（见下方落库一段）：一条 Result（mean 为平均分、report 为
       解读报告、retrieved_contexts 为检索到的片段）与逐条 ResultItem，明细不进状态——状态里带的是编号，
       明细按编号从库里取。

    本节点是图的中断点（见 graph.Evaluator）：出题那一段跑完就停在这里等恢复，
    恢复（带上同一个线程编号、size 传 None 再调 invoke）后才跑，故同一个数据集可以反复评测。
    """
    writer = runtime.stream_writer
    database_id = database_id_of(state)
    dataset_info = state.dataset_info or []
    if not dataset_info:
        logger.warning("evaluate_dataset: empty dataset")
        writer(Step(message="数据集为空，无法评测", status=StepStatus.FAILED, error="数据集为空，无法评测"))
        return Command(goto=END, update={"error": "数据集为空，无法评测"})

    # 刷新表信息：出题时组装的那份可能已经过时（描述、别名、角色被改过，或重新采集过），
    # 而判定与解读都靠它，这里按状态里的表编号重新取一遍（表已被删掉的自然从上下文里去掉）
    table_infos = await build_table_infos(state.table_ids or [], database_id)

    # 表结构作为判定的数据库上下文：判断两条 SQL 是否等价要看字段语义，模型只有知道字段的业务含义才判得准
    schema = render_schema(table_infos)
    # 本节点的每一帧都带上数据集编号（状态里已经有）：调用方随时能把进度对上是哪条数据集
    writer(Step(message=f"正在评测 {len(dataset_info)} 条问题（并发 {EVALUATION_CONCURRENCY}）",
                dataset_id=state.dataset_id))
    semaphore = Semaphore(EVALUATION_CONCURRENCY)

    # 字典以任务为键、条目序号为值：先到的先上报进度，完成后按序号归位，避免完成顺序打乱数据集顺序。
    # 必须用 async for 迭代：as_completed 在普通 for 下产出的是它内部的协程（拿不到任务本身），
    # 是 Python 3.14 起的新语义
    tasks = {create_task(evaluate(item, runtime.context, database_id, schema, semaphore)): index
             for index, item in enumerate(dataset_info)}
    results = []
    async for task in as_completed(tasks):
        results.append((tasks[task], await task))
        writer(Step(message=f"评测进度：{len(results)}/{len(tasks)}", dataset_id=state.dataset_id))

    # 结果按问题原顺序归位：(检索生成的 SQL, 得分, 理由, 检索到的表信息)，顺序与 dataset_info 一致
    evaluations = [result for _, result in sorted(results)]
    mean = sum(score for _, score, _, _ in evaluations) / len(evaluations)
    logger.info(f"evaluate_dataset: {sum(score for _, score, _, _ in evaluations):.0f}/{len(evaluations)} "
                f"item(s) equivalent, mean={mean:.4f}")
    writer(Step(message=f"评测完成：SQL 等效率 {mean:.4f}", status=StepStatus.SUCCESS,
                dataset_id=state.dataset_id))

    # 解读报告：把表元信息与本轮逐条结果交给模型，写一段「整体结论 + 逐条归因 + 共性问题 + 改进建议」，
    # 随结果一起落库（Result.report）。报告只是解读，生成失败不影响这一轮评测结果本身。
    writer(Step(message="正在生成评测解读报告", dataset_id=state.dataset_id))
    payload = {
        "mean": mean,
        "items": [
            {
                "question": item.question,
                "reference_sql": item.sql,
                "response_sql": answer,
                "score": score,
                "reason": reason,
            }
            for item, (answer, score, reason, _) in zip(dataset_info, evaluations)
        ],
        "tables": [asdict(x) for x in table_infos],
    }
    report, error = await invoke(runtime.context.model, [
        SystemMessage(content=EVALUATION_REVIEW_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], EvaluationReview)
    if error:
        logger.error(error)
        writer(Step(message=f"解读报告生成失败：{error}", status=StepStatus.FAILED, error=error,
                    dataset_id=state.dataset_id))

    # 检索到的表信息：逐条问题检索出来的表按问题顺序依次排开，随结果一起落库、不去重（同一张表被多条问题
    # 检索到就存多份，各条问题检索到什么就是什么），作为这一轮的检索片段快照
    # （见 models.dataset.Result.retrieved_contexts）。表信息在 evaluate 里已逐条转成字典。
    retrieved_contexts = [table_info for _, _, _, table_infos in evaluations for table_info in table_infos]

    # 落库：一条 Result（平均分、解读报告与检索到的片段）加逐条 ResultItem（参考答案、待评 SQL 与判定理由）。
    # 参考答案直接从本轮的数据集条目抄下来：dataset_info 与 evaluations 同序、一一对应（见上面按任务编号归位），
    # 存下参考 SQL 就不必再回表取条目。结果与条目在一个事务里写入，避免留下没有条目的评测结果。
    async with in_transaction(META_CONNECTION):
        result = await Result.create(dataset_id=state.dataset_id, mean=mean, report=report,
                                     retrieved_contexts=retrieved_contexts)
        await ResultItem.bulk_create([
            ResultItem(result_id=result.id, reference=item.sql, answer=answer, score=score, reason=reason)
            for item, (answer, score, reason, _) in zip(dataset_info, evaluations)
        ])
    logger.info(f"evaluate_dataset: saved result {result.id} of dataset {state.dataset_id}")
    writer(Step(message=f"评测结果已保存：SQL 等效率 {mean:.4f}", status=StepStatus.SUCCESS,
                dataset_id=state.dataset_id))
    # 把刷新后的表信息写回状态：这一轮判定与解读用的就是它，下一轮恢复时也在它的基础上再刷新
    return Command(update={"table_infos": table_infos})
