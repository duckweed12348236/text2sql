"""检索、采集与评估共用的数据结构：状态、进度帧与各节点的模型输出。

放在同一份里是为了让「同一个概念只有一处定义」（Step / StepStatus / TableInfo / ColumnInfo 等几处原来各
有一份），也让 checkpointer 与提示词只需要认一个模块。按用途分节，节内顺序与使用顺序一致。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Annotated, Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, RootModel, model_validator

from models.meta import TableRoleOption, ColumnRoleOption
from plugins.agents.models import JSONOutputValidator, RerankModel
from schemas import Schema


# ── 公共：状态合并、模型输出清洗、位置数组兼容 ────────────────────────────────────────────────

def keep_latest(left: Any, right: Any) -> Any:
    """同名通道被写多次时取最后一次写入（用作 Annotated 的 reducer）。

    LangGraph 的默认通道只允许「一步写一次」：并行分支、回退重跑、中断恢复都可能让同一个字段在同一步
    被写两次，撞上就是 InvalidUpdateError（INVALID_CONCURRENT_GRAPH_UPDATE）。这里声明「后写覆盖先写」，
    只用在「各分支算出来的是同一件事的最新结果」的字段上（见 State.table_infos / final_table_infos /
    metric_infos）。
    """
    return right


def strip_fence(text: str) -> str:
    """去掉模型偶尔附加的 Markdown 代码块围栏（```json ... ```）。

    只负责「文本 → 文本」的清洗，解析与校验交给各模型自己的
    validate_output：LLM 输出统一按 JSON 文本校验。
    """
    content = text.strip()
    if content.startswith("```"):
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return content


def _pad(values: Any, size: int) -> list[Any]:
    """把位置数组补齐到固定长度（缺少的下标补 None），采集侧解析模型输出的位置数组用。"""
    values = list(values) if isinstance(values, (list, tuple)) else [values]
    return (values + [None] * size)[:size]


def _pick(source: dict[str, Any], *names: str) -> Any:
    """按候选字段名取值，用于兼容模型偶发的「对象」输出（采集侧）。"""
    for name in names:
        if name in source:
            return source[name]
    return None


class Mode(StrEnum):
    """检索模式。

    FULL（完整模式）：走完整流程，先做意图识别，SQL 校验时调用大模型判断查询是否合规；
    EVALUATION（评估模式）：跳过意图识别节点，且 SQL 校验只做 EXPLAIN 语法校验、不调用大模型。
    """
    FULL = "full"
    EVALUATION = "evaluation"


@dataclass()
class Context:
    """图运行上下文：由 StateGraph(context_schema=Context) 注入为 runtime.context。

    节点通过 runtime.context.model 取用 LLM，通过 runtime.context.embedding_model 取用嵌入模型，
    通过 runtime.context.rerank_model 取用重排模型（重排客户端本身不绑定模型，每次调用传入它）。

    检索哪个库、哪种模式属于这一轮的输入、随状态走（见 State.database_id / State.mode），
    不放上下文里——上下文只装那些与状态无关、每轮都一样的依赖。
    """
    model: BaseChatModel
    embedding_model: Embeddings
    rerank_model: RerankModel


@dataclass()
class State:
    """检索图的状态。

    question：输入，这一轮要转成 SQL 的自然语言问题，各节点都围着它干活。

    database_id：输入（必填），本次检索的业务库编号（Database.id）：字段与取值都在该库的向量集合里
        召回（见 Qdrant.find_columns / find_values），补充库信息、校验与执行 SQL 也连它，不跨库。

    mode：输入，检索模式（见 Mode）：整轮检索按它决定是否走意图识别与模型合规校验。

    clarification：反问用户的那句话。意图模糊时是「把问题说清楚」，指标口径对不上时是「说明指标口径」——
        两种情况共用这一个通道，前端拿到的都是同一句「请补充」；这些都落进 checkpoint，
        所以「问一句 → 用户答一句 → 接着跑」能跨请求接上（见 nodes.analyse_intention / validate_metrics /
        ask_metric_definition）。

    metric_definition：用户补充的指标口径（反问通过后写入），作为这一轮已经澄清过的口径留在状态里；
    retry_count：纠错与反问共用的重试计数——指标口径每被驳回一次加一（上限见 nodes.MAX_ASK_RETRY），
        口径确认后清零，后面 SQL 纠错再从这里从零开始数（上限见 nodes.MAX_RETRY）。

    probe_count：已经因为「缺表 / 缺字段」重召回过几次（上限见 nodes.MAX_RECALL_RETRY），
        与 retry_count 分开记，免得挤占 SQL 纠错的预算。

    table_infos：**这一轮**召回出来的候选表（含字段与取值），只给重排用：召回节点按关键词定位到表之后，
        把表连同它名下的全部字段组装进来（见 nodes.recall_columns）；重排完就交出结果，后续节点不再读它；
    final_table_infos：**跨轮累积**的候选表：细筛之后把本轮选中的表合并进来（按表编号去重，见
        nodes.filter_tables / merge_table_infos），此后的生成 SQL 与识别关系都以它为准——
        重召回时新关键词未必还能命中上一轮已经找到的表，留着它们才不会越查越偏。

    original_question：用户这一轮的**原始问题**（意图识别之后、改写之前的那一版，见 nodes.analyse_intention）：
        触发过重召回之后，生成 SQL 用的是它而不是重召回临时改写的问题（见 nodes.generate_sql），
        免得 SQL 跟着临时问题跑偏；
    original_keywords：首轮抽出来的关键词（见 nodes.extract_keywords）：重召回时用来对照「缺了什么」；
    probe_question / probe_keywords：**重召回**这一轮用的问题与关键词（见 nodes.generate_sql 的补召回）：
        有值时说明本轮是补召回跑出来的，`question` / `keywords` 也被换成了这两个值供召回与重排使用，
        而生成 SQL 仍回到 original_question。这一版问题只服务于召回，可能与原问题完全不同
        （例如原问题问员工姓名、缺部门表时只问「有哪些部门？」），不必担心它把 SQL 带偏。

    其余字段是各节点跑出来的中间结果（关键词、指标、生成的 SQL 与重试次数），由节点自己写入与取用。

    table_infos / final_table_infos / metric_infos 带 keep_latest reducer：这几份都是「算到现在的最新结果」，
    并行分支（字段 / 指标两路）与补召回的重跑都可能在同一步再写一遍，默认通道遇到同一步两次写入会直接抛
    InvalidUpdateError（见 keep_latest）。
    """
    question: str
    database_id: int
    mode: Mode = Mode.FULL
    keywords: list[str] | None = None
    error: str | None = None
    clarification: str | None = None
    metric_infos: Annotated[list[MetricInfo] | None, keep_latest] = None
    table_infos: Annotated[list[TableInfo] | None, keep_latest] = None
    final_table_infos: Annotated[list[TableInfo] | None, keep_latest] = None
    original_question: str | None = None
    original_keywords: list[str] | None = None
    probe_question: str | None = None
    probe_keywords: list[str] | None = None
    db_info: DatabaseInfo | None = None
    date_info: DateInfo | None = None
    sql: str | None = None
    retry_count: int = 0
    probe_count: int = 0


# ── 进度帧：检索、采集、评估三张图共用 ────────────────────────────────────────────────────────

class StepStatus(StrEnum):
    """进度状态。

    RUNNING / SUCCESS / ERROR 是检索图的用法：ERROR 表示这一步以失败帧结束整轮检索；
    FAILED 是采集与评估的用法：某一项失败但流程照常往下走（调用方据此继续记日志 / 继续下一批）。
    """
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


@dataclass()
class Step:
    """节点进度：逐条推给调用方的流式消息（检索、采集、评估共用，字段按用途取用）。

    message：该节点正在做的工作，如「正在进行意图识别」，开始与结束时各上报一条；
    status：见 StepStatus，缺省为 RUNNING（评估工具有几帧只报「正在做什么」）；
    error：失败原因（status 为 error / failed 时携带），如校验不通过的理由、SQL 为空、调用异常信息；
    clarification：反问用户的一句话（问题过于模糊、或指标口径对不上时携带）：前端应当把它当作提问展示，
    而不是失败原因——这一帧的 error 为空，用户补充完整问题后重新提问即可；
    thread_id：这一轮的会话编号（每帧都带）：检索图在反问处中断（见 nodes.ask_metric_definition），
    客户端带着它、把用户的回答作为 content 再 POST 一次即为恢复；
    session_id：这一轮所属的会话编号（Session.id，HTTP 检索接口每帧都带）：客户端用它定位会话，
    下一轮带上它即可接回同一个会话（见 controllers.retrieval.submit_question）；
    sql：该节点产出的 SQL（生成、校正、执行成功时携带），供前端展示；
    data：查询结果等需要透出的数据（执行成功时携带）；
    table_infos：本次检索到的表信息（与 SQL 同一帧携带）：重排后最终用于生成这条 SQL 的表、字段与取值，
    评估模式在校验通过那一帧、完整模式在执行完成那一帧随 SQL 一起交出；评估侧据此把「检索到的片段」落库
    （见 models.dataset.Result.retrieved_contexts）；
    dataset_id：评估工具落库的数据集编号（Dataset.id）：出题阶段从数据集落库那一帧起带上，
    评测阶段每帧都带，调用方据此把进度与数据集对上；
    token：本轮到目前为止的 Token 用量（检索接口每帧都带，累计值）：``prompt`` / ``completion`` / ``total``
    三项，取自回调对这些模型调用的统计（见 plugins.agents.callbacks.TimingHandler）；最后一帧的 total
    就是这一轮的总用量，接口据此写进 Turn.tokens、再累加到 Session.tokens。
    """
    message: str
    status: StepStatus = StepStatus.RUNNING
    error: str | None = None
    clarification: str | None = None
    thread_id: str | None = None
    session_id: int | None = None
    sql: str | None = None
    data: Any = None
    table_infos: list[TableInfo] | None = None
    dataset_id: int | None = None
    token: dict[str, int] | None = None


class Intention(JSONOutputValidator, BaseModel):
    """LLM 输出的意图判定结果：continued 表示问题是否适合生成查询，reason 为判定理由。

    clarification：问题过于模糊、定不下查询对象时的一句反问（其余情况为空串），
    由 analyse_intention 原样推给用户，让它把问题补充完整（提示词见 prompts.intention_analysis.md）。

    校验模型输出时用 validate_output，例如：
    Intention.validate_output('{"continued": false, "reason": "问题过于模糊", "clarification": "请问要查哪类数据？"}')。
    """

    continued: bool
    reason: str | None = None
    clarification: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> Intention:
        """清洗代码块围栏后校验模型输出，并返回意图判定结果。"""
        return cls.model_validate_json(strip_fence(text))


class RewrittenEntity(BaseModel):
    """改写问题时补全的一个实体：原文说法 → 补全后的名称 + 推测的实体类型。

    mention 原样保留用户的说法（如「创新发展中心」），expanded 是补全后的名称（如「创新发展中心部门」），
    type 是推测出来的实体类型（部门 / 地区 / 项目 / 产品 / 人员 / 单据等），只用于日志与排查。
    """
    mention: str
    expanded: str
    type: str | None = None


class QuestionRewrite(JSONOutputValidator, BaseModel):
    """LLM 输出的问题改写结果（提示词见 prompts.question_rewrite.md）。

    question：改写后的完整问题，由 rewrite_question 替换 state.question 往下跑——补全了实体类型
    （「创新发展中心」→「创新发展中心部门」）与用户隐含关心的属性（「员工有哪些」→「姓名、工号」）；
    entities / attributes：补了哪些实体、推测了哪些属性，只用于日志排查，不参与后续检索；
    reason：模型对这次改写的一句话说明。

    校验模型输出时用 validate_output，例如：
    QuestionRewrite.validate_output('{"question": "创新发展中心部门的员工有哪些？"}')。
    """

    question: str
    entities: list[RewrittenEntity] | None = None
    attributes: list[str] | None = None
    reason: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> QuestionRewrite:
        """清洗代码块围栏后校验模型输出，并返回改写结果。"""
        return cls.model_validate_json(strip_fence(text))


class RecallProbe(JSONOutputValidator, BaseModel):
    """LLM 对「召回到的表与字段够不够回答问题」的判断（提示词见 prompts.recall_probe.md）。

    sufficient：够用为 True——此时 question / keywords 给空值，照常生成 SQL；不够用为 False；
    question：判为不够用时的新问题，供**重召回**使用（不是给用户的回答）：它可以与原问题完全不同——
    原问题问「员工的姓名和工号」、候选里缺部门表时，这里可能只给「有哪些部门？」，只要能把缺失的表 /
    字段召回即可；生成 SQL 用的仍是原始问题（见 State.original_question）；
    keywords：判为不够用时、聚焦在缺失概念上的召回关键词（中英文、口语说法、可能的表名 / 字段名）；
    missing_tables / missing_columns：模型认为还缺哪些表 / 字段（业务叫法），只用于日志排查；
    reason：判断依据。

    由 generate_sql 在生成 SQL 之前调用（见 nodes.probe_recall）：判为够用就直接往下生成；判为不够用则把
    这一对问题与关键词写进 state.probe_question / state.probe_keywords，并替换 state.question /
    state.keywords 供下一轮召回使用。

    校验模型输出时用 validate_output，例如：
    RecallProbe.validate_output('{"sufficient": false, "question": "部门下有哪些员工？", '
                               '"keywords": ["员工", "employee"]}')。
    """

    sufficient: bool
    question: str | None = None
    keywords: list[str] | None = None
    missing_tables: list[str] | None = None
    missing_columns: list[str] | None = None
    reason: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> RecallProbe:
        """清洗代码块围栏后校验模型输出，并返回召回充分性的判断。"""
        return cls.model_validate_json(strip_fence(text))


class MetricDefinition(BaseModel):
    """一条从用户回答里整理出来的指标：名称 + 规范口径 + 其他说法。

    由审核模型填写（见 prompts.metric_answer_validation.md），ask_metric_definition 逐条落成 Metric 并向量化。
    """
    name: str | None = None
    description: str | None = None
    alias: list[str] | None = None


class MetricAnswer(JSONOutputValidator, BaseModel):
    """LLM 对「用户补充的指标定义」的审核结果：accepted 表示这段回答能不能用作指标口径。

    clarification：不能采纳时再问用户的一句（其余情况为空串），由 ask_metric_definition 写回状态、
    跳回自己再问一次（提示词见 prompts.metric_answer_validation.md）。

    metrics：采纳时模型整理出的指标列表——用户一句话可能补齐好几个指标的口径，一条回答整理出多条是常态；
    由 ask_metric_definition 逐条落成 Metric 并向量化，下次同类问题就能直接召回。不能采纳时为空数组。

    校验模型输出时用 validate_output，例如：
    MetricAnswer.validate_output('{"accepted": false, "reason": "敷衍", "clarification": "换个问法…？"}')。
    """

    accepted: bool
    reason: str | None = None
    clarification: str | None = None
    metrics: list[MetricDefinition] | None = None

    @classmethod
    def validate_output(cls, text: str) -> MetricAnswer:
        """清洗代码块围栏后校验模型输出，并返回回答审核结果。"""
        return cls.model_validate_json(strip_fence(text))


class MetricMatch(JSONOutputValidator, BaseModel):
    """LLM 输出的指标过滤结果：先认出用户输入涉及哪些指标，再判断召回的候选里哪些与之一致。

    required：用户输入涉及的指标说法（用用户自己的说法），问题不需要指标（明细、列表、单号、纯计数）时为空；
    indexes：口径与 required 里某些说法一致的候选指标下标（对应输入 metrics 的位置，可能多个、可能为空）；
    missing：required 里没有任何候选项对得上的说法——库里的口径都没覆盖到，需反问用户补定义；
    clarification：missing 非空时反问用户定义的一句话，其余情况为空串。

    校验模型输出时用 validate_output，例如：
    MetricMatch.validate_output('{"required": ["销售额"], "indexes": [0], "missing": [], "reason": ""}')。
    """

    required: list[str] | None = None
    indexes: list[int] | None = None
    missing: list[str] | None = None
    reason: str | None = None
    clarification: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> MetricMatch:
        """清洗代码块围栏后校验模型输出，并返回指标过滤结果。"""
        return cls.model_validate_json(strip_fence(text))


class SQLValidation(JSONOutputValidator, BaseModel):
    """LLM 输出的 SQL 合规校验结果：continued 表示查询是否放行，reason 为放行 / 拒绝的理由。

    校验模型输出时用 validate_output，例如：
    SQLValidation.validate_output('{"continued": false, "reason": "禁止写入"}')。
    """

    continued: bool
    reason: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> SQLValidation:
        """清洗代码块围栏后校验模型输出，并返回合规校验结果。"""
        return cls.model_validate_json(strip_fence(text))


class KeywordList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的关键词字符串列表：根类型即 list[str]，校验的是「裸列表」本身。

    校验模型输出时用 validate_output，例如：
    KeywordList.validate_output('["下单时间", "订单金额"]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class TableIdList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的表过滤结果：保留的表编号列表（提示词见 prompts.table_filtering.md）。

    根类型即 list[str]，校验的是「裸列表」本身，校验模型输出时用
    validate_output，例如：
    TableIdList.validate_output('["1735600000000000001.orders"]')。
    输出只给编号、不复述表内容：编号回 state.final_table_infos 取表，模型没机会改写表的信息。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class RelationItem(BaseModel):
    """一条表间关联：源表的某个字段连到目标表的某个字段（两者可做等值 JOIN）。

    表用表编号、字段用字段名，与提示词（prompts.relation_recognition.md）的输出一致；
    落库前由 recognise_relations 换成 `<表名>.<字段名>` 并校验两端都在这批表里。
    """
    source_table: str
    source_column: str
    target_table: str
    target_column: str


class RelationList(JSONOutputValidator, RootModel[list[RelationItem]]):
    """LLM 输出的关系识别结果：多张表之间可关联的字段对。

    根类型即 list[RelationItem]，校验的是「裸列表」本身，校验模型输出时用
    validate_output，例如：
    RelationList.validate_output('[{"source_table": "1735600000000000001.orders",
    "source_column": "user_id", "target_table": "1735600000000000001.users",
    "target_column": "id"}]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[RelationItem]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


@dataclass()
class TableInfo:
    id: str
    name: str
    role: TableRoleOption
    description: str
    columns: list[ColumnInfo] | None = None

    def dump(self) -> dict[str, Any]:
        """转成提示词里的表格结构（各提示词的「输入格式」以它为准）。

        字段上不带 `table_id`：所属表已由外层的表给出，字段里再写一遍只是噪声，提示词的 Schema 里也不列它。
        """
        payload = asdict(self)
        for column in payload["columns"] or []:
            column.pop("table_id", None)
        return payload


@dataclass()
class ColumnInfo:
    """检索阶段的字段信息。

    values：该列在数据库中的取值（元数据库里登记的样本 + 同步字段从向量库 values 集合召回的取值，
    合并去重，见 nodes.recall_columns）；该列没采过取值时为空列表。

    related_columns：该字段可用于关联查询的对端字段，命名格式 `<表名>.<字段名>`
    （元数据库里已登记的关系，由关系识别节点写入，见 recognise_relations）；没有识别过时为空列表。
    """
    id: str
    name: str
    type: str
    description: str
    alias: list[str]
    table_id: str
    values: list[Any] | None = None
    role: ColumnRoleOption | None = None
    related_columns: list[str] | None = None


@dataclass()
class MetricInfo:
    """指标信息：id 取自元数据库的 Metric.id，即雪花 ID（整数）。

    id 只在本系统内部用（回表、落库），提示词里不带它——口径审核只看名称、描述与别名（见 dump）。
    """
    id: int
    name: str
    description: str
    alias: list[str]

    def dump(self) -> dict[str, Any]:
        """转成提示词里的指标结构：去掉 id，模型只按名称 / 描述 / 别名判断口径。"""
        payload = asdict(self)
        payload.pop("id", None)
        return payload


@dataclass()
class DatabaseInfo:
    dialect: str
    version: str
    charset: str


@dataclass()
class DateInfo:
    now: str
    week_day: int
    quarter: int


# ── 采集：大模型对表 / 列的分析结果 ───────────────────────────────────────────────────────────

class DiscoveredColumn(Schema):
    """大模型对单列的分析结果。

    正常输出是位置数组 [列名, 角色, 描述, 别名, 同步标记]；模型偶尔会改用字段名表示
    （如 {"name": ..., "role": ...}），这里一并兼容。
    """
    name: str
    role: ColumnRoleOption
    description: str = ""
    alias: list[str] = []
    synchronized: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, alias, synchronized = _pad(value, 5)
            return {"name": name, "role": role, "description": description or "",
                    "alias": alias or [], "synchronized": bool(synchronized)}
        if isinstance(value, dict):
            return {"name": _pick(value, "name", "column_name", "column", "field"),
                    "role": _pick(value, "role", "column_role"),
                    "description": _pick(value, "description", "comment", "column_comment") or "",
                    "alias": _pick(value, "alias", "aliases") or [],
                    "synchronized": bool(_pick(value, "synchronized", "is_enum", "enum"))}
        return value


class DiscoveredTable(Schema):
    """大模型对单表的分析结果。

    正常输出是位置数组 [表名, 角色, 表描述, 列数组]；同样兼容字段名（对象）表示。
    """
    name: str
    role: TableRoleOption
    description: str = ""
    columns: list[DiscoveredColumn] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, columns = _pad(value, 4)
            return {"name": name, "role": role,
                    "description": description or "", "columns": columns or []}
        if isinstance(value, dict):
            columns = _pick(value, "columns", "column_infos", "fields")
            if isinstance(columns, dict):
                columns = list(columns.values())
            return {"name": _pick(value, "name", "table_name", "table"),
                    "role": _pick(value, "role", "table_role"),
                    "description": _pick(value, "description", "comment", "table_comment") or "",
                    "columns": columns or []}
        return value


class DiscoveredTableList(RootModel[list[DiscoveredTable]]):
    """大模型输出的表分析结果：根类型即 list[DiscoveredTable]，校验的是「裸列表」本身。

    与检索侧的 KeywordList / TableIdList 同一写法：
    - model_validate([element])：收原始列表，用于整体解析失败时逐个元素抢救（见 nodes._salvage_tables）；
    - model_validate_json(content)：收模型返回的 JSON 文本，用于整体解析（见 nodes.analyze），
      解析失败抛 ValidationError，调用方据此区分截断与格式错误。
    """


# ── 评估：出题 / 批量生成与纠正 SQL / 评测解读 ────────────────────────────────────────────────

class StringList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的字符串数组：根类型即 list[str]，校验的是「裸列表」本身（出题用）。

    校验模型输出时用 validate_output，例如：
    StringList.validate_output('["t_order", "t_user"]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class SqlRecord(BaseModel):
    """单条 SQL 结果：问题标识 key（整数）与对应的 SQL。"""

    key: int
    sql: str


class SqlList(JSONOutputValidator, RootModel[list[SqlRecord]]):
    """LLM 输出的 SQL 列表：每项携带问题标识 key（整数）与对应 SQL。

    校验模型输出时用 validate_output，例如：
    SqlList.validate_output('[{"key": 1, "sql": "SELECT 1"}]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[SqlRecord]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class EvaluationReview(JSONOutputValidator):
    """LLM 输出的一轮评测解读报告（落进 Result.report）：整体结论、逐条归因、共性问题与改进建议。

    报告是 **纯文本**（Markdown 正文），模型直接输出正文、不做 JSON 包装，因此这里只把文本清洗一下，
    没有可校验失败的结构——配合 invoke 用时，重试只会发生在调用本身出错（限流、超时）的场合。
    """

    @classmethod
    def validate_output(cls, text: str) -> str:
        """返回报告正文：去掉首尾空白，以及模型偶尔自作主张加的代码块围栏。"""
        return strip_fence(text)


@dataclass()
class DatasetItemInfo:
    """评估数据集的一条明细：问题与对应的参考 SQL（落库前后都用它传递）。"""
    question: str
    sql: str


# 评测的逐条结果用元组传递（检索生成的 SQL、等效性得分、得分理由、检索到的表信息，见 nodes.evaluate）：
# 它只服务于落库那一刻，不再单独建模，也不进图状态。
