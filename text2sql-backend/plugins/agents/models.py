from dataclasses import dataclass
from abc import ABC, abstractclassmethod
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.messages import AIMessage, AnyMessage, HumanMessage
from loguru import logger

from config import DOUBAO_MODEL_KEY, DOUBAO_MODEL_NAME, DOUBAO_MODEL_URL, \
    DEEPSEEK_MODEL_KEY, DEEPSEEK_MODEL_NAME, DEEPSEEK_MODEL_URL, \
    BGE_LARGE_ZH_MODEL_KEY, BGE_LARGE_ZH_MODEL_NAME, BGE_LARGE_ZH_MODEL_URL, \
    QWEN_RERANK_MODEL_KEY, QWEN_RERANK_MODEL, QWEN_RERANK_URL, QWEN_RERANK_INSTRUCT_FIELD


@dataclass()
class RerankModel:
    """重排模型的连接信息，随图上下文传入重排客户端。

    url：完整请求地址。OpenAI 官方协议没有重排端点，「OpenAI 兼容」的各家路径并不统一
        （百炼是 {base}/compatible-api/v1/reranks，硅基流动是 {base}/v1/rerank），故直接给出完整地址；
    name：模型名，如 Qwen/Qwen3-Reranker-8B、BAAI/bge-reranker-v2-m3；
    key：该服务商的 API Key；
    instruct_field：任务说明（instruction）在请求体里的字段名，各家叫法不统一
        （硅基流动为 instruction，百炼为 instruct）；None 表示不发该字段。
    """
    url: str
    name: str
    key: str
    instruct_field: str | None = "instruct"


# 输出上限在调用处显式传入：ainvoke(..., max_tokens=MAX_TOKENS)，常量定义在各 agent 模块内
# 结构化输出（整库表/列语义）体积大，服务端默认值过小会被静默截断，表现为返回的 JSON 不完整。
# 取当前模型的最大输出长度：豆包 doubao-seed-2-1-pro 的 max_output_token_length = 262144。
# 注意：切换 model 时需同步调整（DeepSeek 官方端点上限仅 8192）。
# 调用时关闭思考模式：方舟默认开启思考，思维链会吃满输出预算（正文返回空）并大幅拉长耗时
# 用法：ainvoke(..., extra_body=NO_THINKING)
# request_timeout：方舟对思考模型建议客户端超时 ≥1800s，默认 600s 会中途超时
doubao = ChatOpenAI(
    api_key=DOUBAO_MODEL_KEY,
    model=DOUBAO_MODEL_NAME,
    base_url=DOUBAO_MODEL_URL,
    temperature=1,
    request_timeout=1800
)

# DeepSeek 经方舟同一端点调用，备选模型
deepseek = ChatOpenAI(
    api_key=DEEPSEEK_MODEL_KEY,
    model=DEEPSEEK_MODEL_NAME,
    base_url=DEEPSEEK_MODEL_URL,
    temperature=1,
    request_timeout=1800
)

# 各 agent 实际使用的模型：当前为豆包
model = doubao

# 嵌入模型走硅基流动的 OpenAI 兼容端点：BAAI/bge-large-zh-v1.5 输出 1024 维，
# 与 Qdrant 各集合的向量维度（VectorParams(size=1024)）一致。
# check_embedding_ctx_length=False：默认会先用 tiktoken 把文本编码成 token 数组再发请求，
# 只有 OpenAI 官方端点接受这种入参，第三方兼容端点必须直接传文本。
# api_key 缺省时给占位串：OpenAIEmbeddings 在构造阶段校验 key，留空会让整个应用的 import 失败
# （plugins.agents → retrieval.nodes → 本模块），占位后应用可正常启动，只是首次调用嵌入会报鉴权错误。
bge_large_zh_embeddings = OpenAIEmbeddings(
    api_key=BGE_LARGE_ZH_MODEL_KEY,
    model=BGE_LARGE_ZH_MODEL_NAME,
    base_url=BGE_LARGE_ZH_MODEL_URL,
    check_embedding_ctx_length=False
)

# 重排模型：由检索图在 invoke 时放进 Context，节点从 runtime.context.rerank_model 取用。
# 换服务商或模型只改 config 里的 QWEN_RERANK_*（当前为硅基流动的 Qwen/Qwen3-Reranker-8B，
# 备选的 BAAI/bge-reranker-v2-m3 见 BGE_RERANKER_V2_M3_MODEL_*）。
qwen_rerank = RerankModel(url=QWEN_RERANK_URL, name=QWEN_RERANK_MODEL, key=QWEN_RERANK_MODEL_KEY,
                          instruct_field=QWEN_RERANK_INSTRUCT_FIELD)


class JSONOutputValidator(ABC):
    """大模型 JSON 输出的校验接口：各模型自己把模型返回的文本解析成结构化结果。

    校验失败一律抛异常（解析错误、字段缺失、类型不符），调用方据此重试（见 invoke）。
    """

    @abstractclassmethod
    def validate_output(cls, text: str) -> Any:
        pass


# 校验失败后的重试指令：连同模型的上一版输出一起作为新一轮对话回传，让它据此自我纠正
RETRY_INSTRUCTION = "上一次输出未通过校验，请按下面的错误信息修正后重新输出" \
                    "（只输出符合要求的 JSON，不要任何解释）：\n{error}"


async def invoke(model: BaseChatModel,
                 messages: list[AnyMessage],
                 validator: type[JSONOutputValidator],
                 max_retry: int = 3,
                 **kwargs) -> tuple[Any, str | None]:
    """调用大模型并校验其输出，校验不过就带上错误信息重试，超过上限返回错误文本而不抛异常。

    :param model: 大模型实例
    :param messages: 本轮对话消息
    :param validator: 输出校验器（见 JSONOutputValidator），负责把模型返回的文本解析成结构化结果
    :param max_retry: 最多尝试几次（含首次），至少为 1
    :param kwargs: 透传给 model.ainvoke 的参数，如 max_tokens、extra_body
    :return: (校验后的输出, 错误文本)：成功时错误文本为空串；达到重试上限时输出为 None，
        错误文本为最后一次的失败原因（调用异常或校验异常）

    重试覆盖两类失败：调用本身抛异常（限流、超时等），以及返回文本没过校验。
    后者会把模型的上一版输出与失败原因（见 RETRY_INSTRUCTION）追加进对话，下一轮模型据此纠正；
    追加只发生在本地副本上，不改动调用方传入的消息列表。
    调用方拿到的永远是一对返回值，不必再自己 try / except；取消等 BaseException 不在此吞掉，直接向上抛。
    """
    error = ""
    # 本地的对话副本：校验失败时往里追加「上一版输出 + 错误信息」，调用方的消息列表保持不变
    attempt_messages = list(messages)
    for attempt in range(1, max_retry + 1):
        try:
            response = await model.ainvoke(attempt_messages, **kwargs)
        except Exception as e:  # noqa: BLE001 - 失败原因统一以错误文本返回给调用方
            error = str(e)
            logger.warning(f"invoke: attempt {attempt}/{max_retry} failed: {error}")
            continue
        try:
            return validator.validate_output(response.content), None
        except Exception as e:  # noqa: BLE001 - 校验失败要回传错误信息，故与调用异常分开处理
            error = str(e)
            logger.warning(f"invoke: attempt {attempt}/{max_retry} failed: {error}")
            attempt_messages += [AIMessage(content=response.content),
                                 HumanMessage(content=RETRY_INSTRUCTION.format(error=error))]
    logger.error(f"invoke: retry limit exceeded after {max_retry} attempt(s): {error}")
    return None, error
