"""图运行的回调处理器：用回调事件统计每个节点的耗时、整轮的总耗时与 Token 用量。

langgraph 每执行一个节点都会发 on_chain_start / on_chain_end：run_name 与 metadata 里的
langgraph_node 都是节点名，整张图的运行是这些节点运行的父级。节点的内部调用（模型、嵌入等）
也在同一棵回调树上，同样带 langgraph_node，故按「父运行是否已在计时」把嵌套的排除掉，不重复计入。

同一个节点可能跑多次（指标口径反问、SQL 纠错、结果自查回退），耗时按节点归集成分数列表，
汇总时给出次数与总耗时；总耗时按墙上时间算——节点之外还有 checkpoint 落盘、状态序列化等开销，
不等于各节点耗时之和。

Token 用量走 on_llm_end：每次模型调用带回用量（prompt / completion / total），累加进 tokens。
只统计对话模型——嵌入与重排的用量不在这个回调里（前者走 embed 通道，后者是直接 HTTP），故
tokens 是「检索链路上大模型调用」的用量，不含向量化。
"""

from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from loguru import logger


def extract_usage(response: Any) -> dict[str, int] | None:
    """从一次模型调用的返回（LLMResult）里抠出 token 用量。

    :param response: ``on_llm_end`` 收到的 LLMResult
    :return: ``{"prompt": n, "completion": m, "total": t}``；没带用量时返回 None

    优先取消息上的 ``usage_metadata``（新版 langchain 的统一口径，input/output/total），
    退化到 ``llm_output["token_usage"]``（OpenAI 兼容服务的原始字段）。两条都没有（本地模型、
    报错重试前的空返回等）就当作没统计到，不臆造数字。
    """
    generations = getattr(response, "generations", None) or []
    if generations and generations[0]:
        message = getattr(generations[0][0], "message", None)
        usage = getattr(message, "usage_metadata", None) or {}
        if usage:
            prompt = int(usage.get("input_tokens") or 0)
            completion = int(usage.get("output_tokens") or 0)
            return {"prompt": prompt, "completion": completion,
                    "total": int(usage.get("total_tokens") or prompt + completion)}
    output = getattr(response, "llm_output", None) or {}
    usage = output.get("token_usage") or output.get("usage") or {}
    if usage:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        return {"prompt": prompt, "completion": completion,
                "total": int(usage.get("total_tokens") or prompt + completion)}
    return None


class TimingHandler(BaseCallbackHandler):
    """按节点统计耗时、累计 Token 用量，并在 run 结束时记一条汇总日志。

    用法：把实例放进图运行配置的 callbacks（见 Retriever.invoke），跑完调用 report()：
    ``graph.astream(state, config={"callbacks": [timing]})``，``timing.report()``。

    elapsed：整轮总耗时（秒），从首个回调（图开始）算到调用此刻——中途停在反问上时也能报出已跑的时长；
    nodes：节点名 → 每次执行的耗时（秒），跑过几次就有几个元素；
    tokens：本轮到目前为止的 Token 用量，``prompt`` / ``completion`` / ``total`` 三项累加值
    （统计口径见 :func:`extract_usage`）；检索帧里带的累计值就是它的副本（见 Retriever.invoke）。
    """

    # 只记时间戳，无需异步等待：让回调在发起事件的线程里直接跑，避免线程池调度干扰计时
    run_inline = True

    def __init__(self, name: str = ""):
        super().__init__()
        # name 用于日志区分不同入口的统计（如「检索」「评测」），留空则只记 timing
        self.name = name
        self.nodes: dict[str, list[float]] = {}
        self.tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
        self._running: dict[UUID, tuple[str, float]] = {}
        self._started_at: float | None = None

    @property
    def elapsed(self) -> float:
        """整轮总耗时（秒）：从图开始到此刻，没有回调进来时为 0。"""
        return perf_counter() - self._started_at if self._started_at else 0.0

    def on_chain_start(self, serialized: dict[str, Any], inputs: Any, *,
                       run_id: UUID, parent_run_id: UUID | None = None,
                       metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """记节点运行的开始时刻：整轮起点取首个回调，其余只认带 langgraph_node 且未嵌套在节点里的运行。"""
        if self._started_at is None:
            self._started_at = perf_counter()
        node = (metadata or {}).get("langgraph_node")
        # 没有节点标记的是图本身；父运行已在计时的（节点内部的链）不重复计时
        if not node or parent_run_id in self._running:
            return
        self._running[run_id] = (node, perf_counter())

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """节点跑完：把这一段耗时记到节点名下。"""
        self._finish(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """节点抛错 / 被 interrupt 打断也到此为止：已跑的时间照记，统计因此留得下缺口。"""
        self._finish(run_id)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """一次模型调用结束：把用量累加进来（取不到的调用不计，见 :func:`extract_usage`）。"""
        usage = extract_usage(response)
        if usage is None:
            return
        for key, value in usage.items():
            self.tokens[key] = self.tokens.get(key, 0) + value
        logger.info(f"token: prompt {usage['prompt']} + completion {usage['completion']} "
                    f"= {usage['total']}（本轮累计 {self.tokens['total']}）")

    def _finish(self, run_id: UUID) -> None:
        running = self._running.pop(run_id, None)
        if running is None:
            return
        node, started_at = running
        cost = perf_counter() - started_at
        self.nodes.setdefault(node, []).append(cost)
        logger.info(f"timing: {node} {cost * 1000:.0f} ms")

    def report(self) -> None:
        """记一条汇总日志：各节点的次数与耗时（从高到低排列）、整轮总耗时与 Token 用量。"""
        if self._started_at is None:
            return
        detail = "，".join(
            f"{node} {sum(costs) * 1000:.0f} ms（{len(costs)} 次）"
            for node, costs in sorted(self.nodes.items(), key=lambda x: sum(x[1]), reverse=True)
        )
        prefix = f"timing({self.name})" if self.name else "timing"
        logger.info(f"{prefix}: 总耗时 {self.elapsed * 1000:.0f} ms；"
                    f"Token prompt {self.tokens['prompt']} + completion {self.tokens['completion']} "
                    f"= {self.tokens['total']}；{detail or '没有记录到节点耗时'}")
