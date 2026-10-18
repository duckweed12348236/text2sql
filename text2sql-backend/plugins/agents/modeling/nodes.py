import asyncio
import json
import random
import time

from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from openai import APIConnectionError, APITimeoutError, RateLimitError
from pydantic import ValidationError
from tortoise.transactions import in_transaction
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph.graph import END

from models import VALUE_LIMIT, get_column_values, get_table_infos, get_table_names, is_value_type
from models.meta import Column, Table
from plugins.agents.modeling.prompts import TABLE_DISCOVER_PROMPT
from plugins.agents.modeling.schemas import DiscoveredTable, DiscoveredTableList, State, Step, StepStatus, Context
from plugins.json import dump
from plugins.qdrant import Qdrant, Value

MAX_TOKENS = 262144
NO_THINKING = {"thinking": {"type": "disabled"}}
BATCH_SIZE = 200
META_CONNECTION = "meta"
# 单批的表数上限，同时也是单次落库的粒度：模型每输出一次，就把该批的表与列保存一次
TABLE_DISCOVER_BATCH_SIZE = 20
# 单批的列数预算（与表数上限取先到者）：输出约 46 token/列、1.24 秒/列，200 列 ≈ 4 分钟。
# 批越小，输出越短，格式漂移/截断的概率越低，单次重试的代价也越小。
BATCH_COLUMN_LIMIT = 200
# 解析失败时的拆分下限：组内表数不超过该值就不再拆分，改为消耗重试预算，
# 避免系统性失败（拆了照样失败）把请求数放大成 O(表数)
SPLIT_MIN_TABLES = 5
# 单表重试上限：解析失败与漏输出各自独立计数，超限则跳过（未落库的表下次运行会被重新采集）
MAX_ATTEMPTS = 2
# 单表漏列比例上限：模型分析的字段少于业务库的 80% 就判为这份结果不够用，整张表重新入队重试
# （与漏表共用 MAX_ATTEMPTS 预算）；比例以内则按漏的那几列补空行落库，不为个别字段白跑一次模型
MAX_MISSED_COLUMN_RATIO = 0.2
# 并发调用数：同时分析的批数（discover_tables）与同时向量化的批数（embed_columns）。
# 方舟对突发请求有保护（RequestBurstTooFast），靠 START_INTERVAL 节流
# 与"截断只立即重试前半、后半重新入队等待"来削峰
CONCURRENCY = 5
# 请求最小发出间隔（秒）：与并发配合，让请求量缓慢增长而不是一次性打出
START_INTERVAL = 1.0
# 429 / 连接类错误的退避重试：间隔 10s → 20s → 40s 再放弃（带抖动）
RETRY_LIMIT = 4
RETRY_BASE_DELAY = 10.0
# 向量化节点单次取出的字段数：字段量级在万级，一次全量取出既占内存也让单次 upsert 体积过大
VECTORIZE_BATCH_SIZE = 500
# 取值采集单批的字段数：每批拼成一条 UNION ALL，分支数受服务端 thread_stack 限制
# （256KB 栈实测约 2400 个分支即报 1436 Thread stack overrun），500 留足余量
VALUE_BATCH_SIZE = 500
# 节流状态：保证任意两次请求的发出间隔不小于 START_INTERVAL（方舟对突发请求有保护）
_gate = asyncio.Lock()
_last_start = 0.0
# 写入锁：采集与向量化都是「先删后建」的重写语义（元数据库按表先删后建，向量库按字段先清旧点再写入），
# 同一进程内两个采集任务并发跑同一批表时会互相删掉对方刚写入的数据，故各自的写操作串行化。
# 锁是进程内的：多副本部署时需换成分布式锁（PostgreSQL 咨询锁 / Redis 锁）
_discover_lock = asyncio.Lock()
_vectorize_lock = asyncio.Lock()

qdrant = Qdrant()


def _salvage_tables(content: str) -> tuple[list[DiscoveredTable], int]:
    """整体解析失败时，逐个顶层元素抢救可用的表。

    :return: (成功解析的表, 被丢弃的元素个数)
    """
    if not content.startswith("["):
        return [], 0
    decoder, index, tables, dropped = json.JSONDecoder(), 1, [], 0
    while index < len(content):
        while index < len(content) and content[index] in " \t\r\n,":
            index += 1
        if index >= len(content) or content[index] == "]":
            break
        try:
            element, index = decoder.raw_decode(content, index)
        except ValueError:
            # 元素本身残缺（截断或格式错），无法可靠跳到下一个元素，后面的只能放弃
            dropped += 1
            break
        try:
            tables.extend(DiscoveredTableList.model_validate([element]).root)
        except ValidationError:
            dropped += 1
    return tables, dropped


async def analyze(model, group: list[list]) -> tuple[str, list[DiscoveredTable] | None, Exception | None]:
    """单次模型调用：不抛异常，用返回值 (status, 表结构, 异常) 表达结果。

    :param model: 图运行上下文里注入的 LLM 实例

    - ("ok", 表结构, None)      ：整体解析成功
    - ("partial", 表结构, None) ：只有部分表解析成功（多为被截断），漏掉的交给上层重试
    - ("truncated", None, 异常) ：整体不可解析且输出被截断，应把该批拆小后重试
    - ("invalid", None, 异常)   ：整体不可解析，应重试
    - ("error", None, 异常)     ：429/连接类错误重试后仍失败
    - ("fatal", None, 异常)     ：429/连接类之外的意外异常（400、配额、响应结构异常等），
      同一份输入重试只会得到同一个错误，由上层上报失败后直接结束采集

    本函数不向调用方抛异常：并发调用时一个异常会带走同一波里其它任务的结果，
    所有失败都以返回值表达，由 discover_tables 决定是重试、拆分还是结束。
    """
    global _last_start
    error = None
    for attempt in range(RETRY_LIMIT):
        try:
            # 节流：控制请求发出的节奏，让请求量缓慢增长而不是一次性打出
            async with _gate:
                await asyncio.sleep(max(0.0, _last_start + START_INTERVAL - time.monotonic()))
                _last_start = time.monotonic()
            response = await model.ainvoke([
                SystemMessage(content=TABLE_DISCOVER_PROMPT),
                HumanMessage(content="# 表与列的元数据 ```json " + dump(group, default=str) + " ```")
            ], max_tokens=MAX_TOKENS, extra_body=NO_THINKING)
            # 去掉模型偶尔附加的 Markdown 代码块围栏
            content = response.content.strip()
            if content.startswith("```"):
                lines = content.splitlines()[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
            logger.info(content)
            try:
                return "ok", DiscoveredTableList.model_validate_json(content).root, None
            except ValidationError as e:
                error = e
                # 整体解析失败时逐个元素抢救：截断的那一批往往能救回前面的表，只重试尾部
                salvaged, dropped = _salvage_tables(content)
                if salvaged:
                    logger.warning(f"partial response for {len(group)} table(s): "
                                   f"salvaged {len(salvaged)}, dropped {dropped} element(s)")
                    return "partial", salvaged, None
                if (response.response_metadata or {}).get("finish_reason") == "length":
                    return "truncated", None, e
                return "invalid", None, e
        except (RateLimitError, APIConnectionError, APITimeoutError) as e:
            error = e
            if attempt == RETRY_LIMIT - 1:
                break
            delay = RETRY_BASE_DELAY * 2 ** attempt + random.uniform(0, 2)
            logger.warning(f"transient error, retry {attempt + 2}/{RETRY_LIMIT} in {delay:.0f}s: {e}")
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"unexpected error while analyzing table(s), abort discovery: {e!r}")
            return "fatal", None, e
    logger.error(error)
    return "error", None, error


async def discover_tables(state: State, runtime: Runtime[Context]) -> Command:
    """分析业务数据库结构，并把表、列元信息写入元数据库。

    :param state: 图状态，table_ids 指定本轮采集哪些表（元数据库的表编号，留空表示本库全部表）
    :param runtime: 图运行上下文，LLM 实例取自 runtime.context.model；这些表所属的业务库编号取自
        state.database_id（必填）
    :return: 跳转到 embed_columns 的指令

    1. 先把 state.table_ids 对应到业务库表名：已登记的编号用元数据库里的表名与库归属，
       未登记的编号按首次采集从其前缀里取出表名，留空表示本库全部表；不看元数据库里是否已登记，
       指定的表一律重新采集（已登记的表按「先删后建」覆盖旧数据）。编号未登记又不是
       <database_id>.<表名> 形态、编号归属别的库、或业务库中不存在该表时逐条上报采集失败。
       读取表结构，把「表 / 列 / 键」展平为位置数组；落库时表编号由编号对应关系取回，表名、编号与
       库归属写在各自的字段上，不互相顶替；列以业务库的真实结构为基准对齐模型输出，
       模型漏列 / 编造列名都不会让元数据库的字段集合偏离业务库；
    2. 按「BATCH_COLUMN_LIMIT 列 / TABLE_DISCOVER_BATCH_SIZE 表」双约束分组，
       **模型每输出一次就落库一次**：解析出多少就存多少（含被截断时的部分抢救）；
    3. 整体不可解析时把该批对半拆分重试（被截断一律拆；解析失败只拆大组）；
       漏输出的表、以及漏列超过 MAX_MISSED_COLUMN_RATIO 的表（补空行已无意义）重新入队整张重试，
       解析失败与结果不够用各有独立的 MAX_ATTEMPTS 预算，超限则跳过（跳过的表未落库，下次运行会被重新采集）；
    4. 落库整批在一个事务里完成，且持有 _discover_lock：同一张表重复采集会覆盖旧数据，
       并发的采集任务也不会在同一张表上互相删掉刚写入的数据。

    进度按「已落库表数 / 待采集表数」上报；放弃采集的表单独上报一次失败，注明表名与原因；
    模型输出的本批范围外表名（不是本次请求给它的表）也逐条上报失败并单独记账，不参与落库；
    模型调用撞上非重试类的意外异常（见 analyze 的 fatal）时，逐表上报原因后结束本轮采集，
    不再跑后面排队的批次。
    """
    writer = runtime.stream_writer
    model = runtime.context.model
    database_id = state.database_id
    # 业务库里的表名（去重、保持原顺序）
    available = list(dict.fromkeys(await get_table_names(database_id)))
    # 表编号 → 业务库表名（编号全局唯一，按编号取表就不会撞上其它库的同名表）：
    # - 已登记的编号：取元数据库里的表名，并要求它归属本库（否则是别的库的表，拒收）；
    # - 未登记的编号：说明是首次采集，表名就在编号里（<database_id>.<表名>）；
    # - 编号留空：本库全部表，已登记的沿用原编号，未登记的按 <database_id>.<表名> 生成。
    # 同一表名出现多个编号时保留先出现的那个，避免同一张表被采两遍而主键冲突
    prefix = f"{database_id}."
    failures = []
    if not state.table_ids:
        registered = {table.name: table.id for table in
                      await Table.filter(database_id=database_id).only("id", "name")}
        table_ids = {name: registered.get(name, f"{prefix}{name}") for name in available}
    else:
        registered = {table.id: table for table in
                      await Table.filter(id__in=state.table_ids).only("id", "name", "database_id")}
        table_ids = {}
        for table_id in dict.fromkeys(state.table_ids):
            table = registered.get(table_id)
            if table is None:
                name = table_id.removeprefix(prefix)
                if name and name != table_id:
                    table_ids.setdefault(name, table_id)
                else:
                    failures.append((table_id, f"编号既未登记也不是 <{database_id}>.<表名> 形态"))
            elif table.database_id != database_id:
                failures.append((table_id, f"该表归属数据库 {table.database_id}，不是 {database_id}"))
            else:
                table_ids.setdefault(table.name, table_id)
    for table_id, reason in failures:
        logger.warning(f"table id skipped: {table_id} ({reason})")
        writer(Step(message=f"表 {table_id} 采集失败", status=StepStatus.FAILED, error=reason))
    # 编号对得上、业务库里却没有这张表：编号可能来自别处或写错了，采不到
    for name in [x for x in table_ids if x not in available]:
        del table_ids[name]
        logger.warning(f"table not found in business database, skip: {name}")
        writer(Step(message=f"表 {name} 采集失败", status=StepStatus.FAILED, error="业务库中不存在该表"))
    table_names = list(table_ids)
    logger.info(f"table(s) to discover: {len(table_names)}")
    if not table_names:
        # 没有待采集的表也不在此结束：向量库可能尚未与元数据库对齐（首次运行、集合被重建等），
        # 仍交给 embed_columns 做一次同步（重复执行是重写，不会堆积）
        logger.warning("no table to discover, skip discovery")
        writer(Step(message="没有待采集的表", status=StepStatus.SUCCESS))
        return Command(goto="embed_columns")
    writer(Step(message=f"开始采集表结构：待采集 {len(table_names)} 张表", status=StepStatus.RUNNING))
    # 表 / 列 / 键 展平为位置数组；真实列另存一份（table_columns），落库时按它对齐模型输出与取列类型
    table_columns, table_infos = {}, []
    for table in await get_table_infos(table_names, database_id):
        flat_columns = []
        for column in table["columns"]:
            keys = [["FK", key.get("ref_table"), key.get("ref_column")] if key.get("type") == "FK"
                    else [key.get("type")] for key in column["keys"]]
            flat_columns.append([column["name"], column["type"], column["nullable"], column["default"],
                                 column["extra"], keys, column["comment"]])
        table_columns[table["name"]] = flat_columns
        table_infos.append([table["name"], table["comment"], flat_columns])
    logger.debug(table_infos)
    logger.info(f"flattened {len(table_infos)} table(s) for analysis")

    tables = []
    columns = []
    discovered = set()
    # 解析失败与漏输出各自计数：两者的成因和补救方式不同，混用会互相消耗重试预算
    invalid_attempts = {}
    missing_attempts = {}
    skipped = []
    # 模型输出的本批范围外表名（幻觉、大小写不一致、顺手带上别批的表）：单独记账便于核对模型行为
    unknown = []

    # 分组：列数预算与表数上限双约束取先到者；保持原顺序，相邻的表仍落在同一批
    groups = []
    current = []
    used = 0
    for table in table_infos:
        size = len(table[2])
        if current and (used + size > BATCH_COLUMN_LIMIT or len(current) >= TABLE_DISCOVER_BATCH_SIZE):
            groups.append(current)
            current, used = [], 0
        current.append(table)
        used += size
    if current:
        groups.append(current)
    while groups:
        wave, groups = groups[:CONCURRENCY], groups[CONCURRENCY:]
        logger.info(f"analyzing {sum(len(x) for x in wave)} table(s) in {len(wave)} group(s), "
                    f"{sum(len(x) for x in groups)} table(s) queued")

        # 只有模型调用并发；落库仍串行，避免同一连接上的事务交错
        responses = await asyncio.gather(*(analyze(model, group) for group in wave))

        for index, group in enumerate(wave):
            status, output, error = responses[index]
            # 整体不可解析：被截断（尺寸问题）一律拆小重试；解析失败只拆大组，小组直接走重试预算，
            # 避免系统性失败（拆了照样失败）把请求数放大成 O(表数)。
            # 只让前半立即重试、后半重新入队等待，避免一次失败就把同时在跑的请求数翻倍。
            if len(group) > 1 and (status == "truncated" or
                                   (status == "invalid" and len(group) > SPLIT_MIN_TABLES)):
                mid = len(group) // 2
                logger.warning(f"{status} response for {len(group)} table(s), "
                               f"split in two halves: {mid} + {len(group) - mid} "
                               f"(retry first half, requeue second)")
                groups[0:0] = [group[:mid]]
                groups.append(group[mid:])
                continue

            # 模型调用失败：两类都不静默吞掉，先逐表上报失败原因再中断
            # （error 是 429/连接类错误重试后仍失败，重跑有望成功，故抛出异常让调用方感知；
            #   fatal 是 400、配额、响应结构异常这类意外异常，同一份输入重试还是同一个错，
            #   故上报后直接跳尾节点结束本轮，不再跑后面排队的批次）
            if status == "fatal":
                logger.error(error)
                for table in group:
                    writer(Step(message=f"表 {table[0]} 采集失败", status=StepStatus.FAILED, error=str(error)))
                # 本波排在后面的批次与队列里的批次都不再执行：一并说明还剩多少张表没采到，
                # 免得只看到本批失败、不知道其余的表去了哪（它们没落库，下次运行会被重新采集）
                left = [table for other in wave[index + 1:] + groups for table in other]
                logger.error(f"abort discovery, {len(left)} table(s) left uncollected: "
                             f"{[table[0] for table in left]}")
                writer(Step(message=f"采集中断：{len(left)} 张表未采集", status=StepStatus.FAILED,
                            error=str(error)))
                return Command(goto=END)

            if status == "error":
                logger.error(error)
                for table in group:
                    writer(Step(message=f"表 {table[0]} 采集失败", status=StepStatus.FAILED, error=str(error)))
                raise error

            if output is None:
                # 整批都没解析出来（只有小组会走到这里）→ 整组重试
                attempts = invalid_attempts
                reasons = {table[0]: "模型输出无法解析" for table in group}
            else:
                # 解析成功（含部分抢救）→ 只重试结果不够用的表：整张表没输出（一点信息都没有），
                # 或输出的字段漏得太多（漏掉的列占比超过 MAX_MISSED_COLUMN_RATIO：补空行等于把这张表的
                # 角色 / 描述 / 别名 / 同步标记丢掉一大半，重试一次的收益大于留一份残缺的结果）。
                # 漏列口径与下面的字段对齐一致：只数「业务库有、模型没给」的列，模型多出来的列名
                # 不算漏列（它们走前面「丢弃未知列名」那条路）
                returned_names = {table.name: {column.name for column in table.columns} for table in output}
                attempts = missing_attempts
                reasons = {}
                for table in group:
                    if table[0] not in returned_names:
                        # 整张表都没输出：一点信息都没有，必须重试
                        reason = "模型未输出该表"
                    else:
                        real_columns = table_columns[table[0]]
                        missed = sum(1 for column in real_columns if column[0] not in returned_names[table[0]])
                        reason = (f"模型只分析了 {len(real_columns) - missed}/{len(real_columns)} 个字段，"
                                  f"漏列超过 {MAX_MISSED_COLUMN_RATIO:.0%}"
                                  if missed > MAX_MISSED_COLUMN_RATIO * len(real_columns) else None)
                    if reason is not None:
                        logger.warning(f"insufficient analysis for {table[0]}: {reason}")
                        reasons[table[0]] = reason
            candidates = [table for table in group if table[0] in reasons]
            # 两类失败的计数分开累计：解析失败整组重试、结果不够用只重试那几张表，超限的放弃
            for table in candidates:
                attempts[table[0]] = attempts.get(table[0], 0) + 1
            retry = [table for table in candidates if attempts[table[0]] <= MAX_ATTEMPTS]
            dropped = [table for table in candidates if attempts[table[0]] > MAX_ATTEMPTS]

            if dropped:
                skipped.extend(table[0] for table in dropped)
                logger.error(f"give up {len(dropped)} table(s) after {MAX_ATTEMPTS} attempt(s): "
                             f"{[table[0] for table in dropped]}")
                for table in dropped:
                    writer(Step(message=f"表 {table[0]} 采集失败", status=StepStatus.FAILED,
                                error=reasons[table[0]]))
            if retry:
                logger.warning(f"retry {len(retry)} table(s): {[table[0] for table in retry]}")
                groups[0:0] = [retry]

            if output is None:
                continue

            # 只接受本批请求的表：模型可能"顺手"把别批的表也输出一遍（比如从外键里看出还有张 user 表），
            # 而 (database_id, name) 唯一 +「先删后建」会让这份幻觉覆盖掉真表；更糟的是它会被记进
            # discovered，等真正负责它的那批返回时被当成"重复输出"丢弃，真表反而落不了库。
            # 故范围只按本批表名校验：别批的表名只上报不落库，交给它自己那一批去处理。
            requested = {table[0] for table in group}
            pending = []
            for table in output:
                if table.name not in requested:
                    logger.warning(f"table out of this batch's scope, skip: {table.name}")
                    unknown.append(table.name)
                    writer(Step(message=f"表 {table.name} 采集失败", status=StepStatus.FAILED,
                                error="模型输出了本批请求范围之外的表名，无对应表编号"))
                    continue
                if table.name in reasons:
                    # 结果不够用（漏列过多）：本轮不落库，整张表重新入队重试
                    # （原因在判定时已记日志，超限放弃的也在上面逐表上报过原因）
                    continue
                if table.name in discovered:
                    # 同一张表被采集两次会撞主键、白跑一遍模型，这里去重
                    logger.warning(f"duplicated table in model response, keep the first: {table.name}")
                    continue
                discovered.add(table.name)
                pending.append(table)
            if not pending:
                continue

            # 表与列在同一事务里批量保存：先清理旧数据再写入，重复采集（表已登记）即覆盖旧数据；
            # 加 _discover_lock 让同一进程内的采集任务串行落库：否则并发的任务会互相删掉对方刚写入的表与列。
            # 编号、表名与库归属各归其位：删除与写入都按表编号（已登记的表沿用原编号，覆盖的是同一行），
            # Column.table_id 与 Column.id 的前缀都取编号，业务库表名只用于读结构与取值；
            # Table.database_id 就是本次的 state.database_id（(database_id, name) 唯一，
            # 同一张表在库内只会有一行，重采覆盖的正是这一行）
            pending_ids = [table_ids[table.name] for table in pending]
            # 字段以业务库的真实列为基准对齐模型输出：列是否存在、顺序与类型都由业务库说了算，
            # 模型只负责角色 / 描述 / 别名 / 同步标记：
            # - 模型漏掉的列补一条空分析结果（角色等留空）：否则该列在元数据库里凭空消失，
            #   检索永远召回不到它，模型也就永远不会用它。这里只处理阈值以内的漏列：漏得太多
            #   （见上面的漏列判定）根本不会走到这儿，那张表会整张重试，不落一份残缺的结果；
            # - 模型多出来的列名（幻觉或改名）直接丢弃并记账：放行会写出 type 为空的幽灵列，
            #   检索把这个业务库里不存在的列名交给模型，拼出的 SQL 必然执行失败；
            # - 模型把同一列写两遍时保留第一条。
            # 最后按 Column.id 去重兜底：Column.id 由 (表编号, 字段名) 拼出，一旦两条记录算出同一个
            # 主键，同一条 bulk_create 就会 1062，整个事务回滚、整轮采集中断——代价太大，值得多一道
            # 防线；去重粒度必须带上表编号（即按主键），只按字段名会误删别的表的同名字段
            batch_columns, seen_ids = [], set()
            for table in pending:
                table_id = table_ids[table.name]
                real_columns = table_columns[table.name]
                first = {}
                for analyzed_column in table.columns:
                    if analyzed_column.name in first:
                        logger.warning(f"duplicated column in model response, keep the first: "
                                       f"{table.name}.{analyzed_column.name}")
                        continue
                    first[analyzed_column.name] = analyzed_column
                missed = 0
                for real in real_columns:
                    name, type_ = real[0], real[1]
                    infos = first.pop(name, None)
                    if infos is None:
                        missed += 1
                    row = Column(id=f"{table_id}.{name}", name=name, type=type_,
                                 role=infos.role if infos else None,
                                 description=infos.description if infos else None,
                                 alias=infos.alias if infos else None,
                                 synchronized=infos.synchronized if infos else False,
                                 table_id=table_id)
                    if row.id in seen_ids:
                        logger.warning(f"duplicated column in model response, keep the first: {row.id}")
                        continue
                    seen_ids.add(row.id)
                    batch_columns.append(row)
                # 剩下的就是模型输出、业务库里却没有的列名：不落库，但要在汇总日志里说清楚模型编了什么
                if missed or first:
                    logger.warning(f"column analysis incomplete for {table.name}: missed {missed}/"
                                   f"{len(real_columns)} column(s), dropped unknown column name(s): "
                                   f"{sorted(first)}")
            async with _discover_lock, in_transaction(META_CONNECTION):
                await Column.filter(table_id__in=pending_ids).delete()
                await Table.filter(id__in=pending_ids).delete()
                await Table.bulk_create(
                    [Table(id=table_ids[x.name], name=x.name, role=x.role.value, description=x.description,
                           database_id=database_id)
                     for x in pending], batch_size=BATCH_SIZE)
                await Column.bulk_create(batch_columns, batch_size=BATCH_SIZE)

            tables.extend(pending)
            columns.extend(batch_columns)
            logger.info(f"saved {len(pending)} table(s) and {len(batch_columns)} column(s)")
            writer(Step(message=f"已采集 {len(tables)}/{len(table_names)} 张表", status=StepStatus.RUNNING))

    # 跳过的表没有落库，下次运行会被重新采集；这里汇总一份清单便于针对性重跑
    if skipped:
        logger.error(f"skipped {len(skipped)} table(s): {skipped}")
    # 这些都归属于别的批次，由那一批负责落库；这里只是汇总，便于核对模型的越界行为
    if unknown:
        logger.error(f"model returned {len(unknown)} table(s) out of their batch's scope: {unknown}")
    logger.info(f"discovered {len(tables)} table(s) and {len(columns)} column(s)")
    writer(Step(message=f"表采集完成：{len(tables)}/{len(table_names)} 张，{len(skipped)} 张跳过",
                status=StepStatus.SUCCESS))
    return Command(goto="embed_columns")


async def vectorize(database_id: int, columns: list[Column], embedding_model: Embeddings) -> Exception | None:
    """向量化并写入一批字段：不抛异常，用返回值表达结果，便于并发执行时互不影响。

    :param database_id: 这批字段所属的业务库编号：字段按库分开存，它决定写进哪个字段集合
        （见 Qdrant.add_columns）
    :param columns: 待向量化并写入的这批字段
    :param embedding_model: 嵌入模型，与检索侧同源（见 Qdrant 类说明）
    :return: 成功为 None，失败为异常对象（由调用方逐字段上报原因）

    同一批字段的「清旧点 + 写入」在 Qdrant.add_columns 内部完成，故以批为单位并发：批与批之间字段不
    重叠，各自清的旧点互不相干；取消等 BaseException 不在此吞掉，直接向上抛。
    """
    try:
        await qdrant.add_columns(database_id, columns, embedding_model)
        return None
    except Exception as e:  # noqa: BLE001 - 失败交由调用方上报，单批失败不影响同批其它字段所在的分组
        logger.error(e)
        return e


async def embed_columns(state: State, runtime: Runtime[Context]) -> Command:
    """把元数据库里的字段批量向量化，并把同步字段的取值写入 Qdrant。

    :param state: 图状态，table_ids 指定向量化哪些表的字段（元数据库的表编号，留空表示本库全部表）；
        database_id 是这些字段所属的业务库编号，既用于圈定「留空」时的范围，
        也决定字段与取值写进向量库的哪一套集合
    :param runtime: 图运行上下文，嵌入模型取自 runtime.context.embedding_model
    :return: 结束采集图的指令（本节点是最后一步）

    state.table_ids 与 discover_tables 的采集范围同一份（元数据库的表编号，全局唯一）：按编号取本轮的
    表，过滤 Column.table_id 用编号，采集取值时拼 SQL 用 Table.name 里的业务库表名，两者不混用。
    编号留空表示本库（database_id）的全部已登记表；元数据库里还没有对应行（本轮没采集成功）的表不会有
    字段可向量化。向量库里的字段与取值都按库分开存，本节点写入的都是 database_id 这一套集合
    （columns.<database_id> / values.<database_id>，见 Qdrant.register）。

    字段按 VECTORIZE_BATCH_SIZE 分页取出（state.table_ids 非空时只取这些表的字段），避免全量载入；
    取出的批次再按 CONCURRENCY 个一批同时向量化（每波 CONCURRENCY × VECTORIZE_BATCH_SIZE 个字段），
    一波跑完再取下一波：批与批之间字段不重叠，各自的清旧点互不相干，故并发安全；
    整段向量化持有 _vectorize_lock，同一进程内的采集任务在其中串行重写向量。
    点编号随机生成，写入前会按 payload 的字段编号清掉这批字段的旧点（见 Qdrant.add_columns），
    所以重复执行不会堆积重复点。同步字段在同一遍分页里顺带挑出，不再单独查一次元数据库。

    随后处理这些 Column.synchronized 为 True 且类型属于 VALUE_TYPES 的字段：按 VALUE_BATCH_SIZE
    分批，每批的字段拼成一条 UNION ALL 查询批量取值（见 models.get_column_values），再交给
    Qdrant.add_values 落库；取值入库时转成字符串、原类型记在 Value.type 里，出库由 Qdrant 按类型还原。
    写入前同样按字段编号清掉旧取值点（见 Qdrant.add_values），重复采集即覆盖。

    进度按「已向量化字段数 / 待向量化字段数」上报（每波一次）；某批向量化失败时逐个字段上报失败，
    注明字段名与原因后继续下一批（这些字段的点在 add_columns 里已先被清掉，本次没补上，需重跑补齐）。
    取值采集同样按「已采集取值的字段数 / 待采集字段数」上报，失败批也逐个字段上报原因后继续。
    """
    writer = runtime.stream_writer
    embedding_model = runtime.context.embedding_model
    database_id = state.database_id
    # 按表编号取本轮的表，取「表编号 → 表名」对照：编号全局唯一，按编号取就不会撞上其它库的同名表；
    # 过滤 Column.table_id 用编号（编号正是 Column.table_id 的取值），
    # 采集取值时拼 SQL 用的是业务库表名（见下面 get_column_values 的调用）；
    # 编号留空表示本库（database_id）的全部已登记表
    tables = await (Table.filter(id__in=state.table_ids) if state.table_ids
                    else Table.filter(database_id=database_id)).only("id", "name")
    table_name_of = {table.id: table.name for table in tables}
    if not table_name_of:
        # 没有可向量化的表（编号都还没登记 / 该库还没有表）：没有字段可处理，也不退回全库
        logger.warning(f"no registered table to vectorize, skip: {state.table_ids or database_id}")
        writer(Step(message="没有待向量化的字段", status=StepStatus.SUCCESS))
        return Command(goto=END)
    column_filter = {"table_id__in": list(table_name_of)}
    total = await Column.filter(**column_filter).count()
    logger.info(f"embed_columns: {total} column(s) to vectorize")
    if not total:
        logger.warning("no column to vectorize, skip")
        writer(Step(message="没有待向量化的字段", status=StepStatus.SUCCESS))
        return Command(goto=END)
    writer(Step(message=f"开始向量化字段：待向量化 {total} 个字段", status=StepStatus.RUNNING))

    # 同步字段收集为（表名, 列名, 列编号）：表名由 Column.table_id 对照到元数据库的 Table.name
    # （编号与表名不一定相同，取值采集拼 SQL 用的是表名）；只留后面取值采集要用的字段，
    # 避免常驻整批 Column 对象。未对照到表名（表已被删）的字段直接跳过
    synchronized_column_infos = []
    vectorized = 0
    failed = 0
    # 字段分组并发：每波 CONCURRENCY 批（每批 VECTORIZE_BATCH_SIZE 个字段），取完一波同时向量化，
    # 等这一波全部结束再取下一波（分页游标依赖元数据库当前的字段序列，取太远容易在多波之间错位）。
    # 批与批之间字段不重叠，各自的清旧点互不相干；整段写入持有 _vectorize_lock，
    # 让同一进程内的采集任务串行重写向量，避免并发的任务删掉对方刚写入的点
    wave_size = VECTORIZE_BATCH_SIZE * CONCURRENCY
    async with _vectorize_lock:
        for start in range(0, total, wave_size):
            batches = [await Column.filter(**column_filter).order_by("id").offset(offset).limit(VECTORIZE_BATCH_SIZE)
                       for offset in range(start, min(start + wave_size, total), VECTORIZE_BATCH_SIZE)]
            errors = await asyncio.gather(*(vectorize(database_id, batch, embedding_model)
                                            for batch in batches))
            for batch, error in zip(batches, errors):
                if error is None:
                    vectorized += len(batch)
                else:
                    failed += len(batch)
                    for column in batch:
                        writer(Step(message=f"字段 {column.id} 向量化失败", status=StepStatus.FAILED, error=str(error)))
                synchronized_column_infos.extend((table_name_of[x.table_id], x.name, x.id) for x in batch
                                                 if x.synchronized and is_value_type(x.type)
                                                 and table_name_of.get(x.table_id))
            logger.info(f"embed_columns: {min(start + wave_size, total)}/{total}")
            writer(Step(message=f"已向量化 {vectorized}/{total} 个字段", status=StepStatus.RUNNING))
    logger.info(f"embed_columns: vectorized {total} column(s), "
                f"{len(synchronized_column_infos)} synchronized column(s) to collect values")

    writer(Step(message=f"字段向量化完成：{vectorized}/{total} 个，{failed} 个失败", status=StepStatus.SUCCESS))

    # 取值采集按批上报进度；某批失败时逐个字段上报原因后继续下一批
    # （这些字段的旧取值点已在 add_values 里先被清掉，本次没补上，需重跑补齐）
    value_total = len(synchronized_column_infos)
    if not value_total:
        logger.info("embed_columns: no synchronized column to collect values")
        writer(Step(message="没有需要采集取值的字段", status=StepStatus.SUCCESS))
        return Command(goto=END)
    writer(Step(message=f"开始采集取值：待采集 {value_total} 个字段的取值", status=StepStatus.RUNNING))

    collected = 0
    collect_failed = 0
    for start in range(0, value_total, VALUE_BATCH_SIZE):
        batch = synchronized_column_infos[start:start + VALUE_BATCH_SIZE]
        try:
            # 每批的字段拼成一条查询批量取值：传入业务库表名（不是表编号），结果按「表名.列名」归组返回，
            # 与 get_column_values 里据 SQL 中的表名拼出的键一致
            values = await get_column_values([(table_name, name, VALUE_LIMIT, 0) for table_name, name, _ in batch],
                                            database_id)
            points = [
                Value(payload=value, column_id=column_id, type=type(value).__name__)
                for table_name, name, column_id in batch
                for value in values.get(f"{table_name}.{name}", [])
                # NULL 不是可用取值，全文索引也不会收录，直接丢弃
                if value is not None
            ]
            async with _vectorize_lock:
                await qdrant.add_values(database_id, points)
            collected += len(batch)
            logger.info(f"embed_columns: values {min(start + VALUE_BATCH_SIZE, value_total)}/"
                        f"{value_total} column(s), {len(points)} value(s)")
        except Exception as e:
            logger.error(e)
            collect_failed += len(batch)
            for _, _, column_id in batch:
                writer(Step(message=f"字段 {column_id} 取值采集失败", status=StepStatus.FAILED, error=str(e)))
        writer(Step(message=f"已采集 {collected}/{value_total} 个字段的取值", status=StepStatus.RUNNING))
    logger.info(f"embed_columns: collected values for {collected}/{value_total} column(s)")
    writer(Step(message=f"取值采集完成：{collected}/{value_total} 个字段，{collect_failed} 个字段失败",
                status=StepStatus.SUCCESS))
    return Command(goto=END)
