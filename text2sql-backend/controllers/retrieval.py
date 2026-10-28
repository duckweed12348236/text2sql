from dataclasses import asdict
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from tortoise.functions import Count, Max

from models.session import Session, Turn
from plugins.agents.graph import Retriever
from plugins.json import dump, load
from plugins.snowflake import Snowflake
from schemas.retrieval import Question

router = APIRouter(prefix="/retrieval")
retriever = Retriever()


async def resolve_session(session_id: int | None) -> Session:
    """按会话编号取会话；没带编号就新建一个会话。

    :param session_id: 上一轮响应里带回来的会话编号（Session.id）；为空表示开一轮新的对话
    :return: 这一轮所属的会话

    新会话的 thread_id 现生成一个雪花 ID（见 models.session.Session.thread_id）：它是检索图那边的会话编号，
    由服务端持有、客户端不必关心——客户端只认 session_id，会话到 thread_id 的对应关系由这里查。
    带了编号却查不到（被删了或拼错）按 404 报，不要悄悄新建一个，否则客户端以为接着上一轮、实际换了一轮。
    """
    if session_id is None:
        return await Session.create(thread_id=Snowflake.get_id())
    session = await Session.get_or_none(id=session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的会话不存在")
    return session


async def start_turn(session: Session, content: str) -> Turn:
    """登记这一轮：轮次接在该会话已有轮次之后。

    :param session: 这一轮所属的会话（见 resolve_session）
    :param content: 用户这一轮的输入，原样保存
    :return: 新建的轮次（steps 先留空，整轮跑完再写回，见 save_steps）

    轮次号取「当前最大 + 1」，同一会话从 1 开始递增；(session_id, round) 在库里唯一，并发提交同一会话时由
    唯一约束兜底（撞上了这一轮就写不进去，属于调用方并发用错，不做自动重排）。
    """
    latest = await Turn.filter(session_id=session.id).order_by("-round").first()
    return await Turn.create(session_id=session.id, round=(latest.round + 1 if latest else 1),
                             input=content, steps=[])


async def save_steps(session: Session, turn: Turn, frames: list[dict[str, Any]]) -> None:
    """把这一轮收集到的进度帧与 Token 用量写回轮次，并把用量累加到会话上。

    :param session: 这一轮所属的会话（见 resolve_session）：累计用量写在它的 tokens 上
    :param turn: start_turn 建好的轮次
    :param frames: 已经推给客户端的帧（JSON 结构，见 output）

    用量取最后一帧的 ``token.total``（帧上的字段叫 ``token``，模型上的列叫 ``tokens``）：检索侧每帧都带
    「到目前为止」的累计值（见 Retriever.invoke），最后一帧即本轮总量；一帧都没有（还没开跑就出错）就记 0。
    轮次的用量与会话的累计在一次写入里完成，写不进去只记日志，不影响已经推出去的帧、也不影响检索本身。
    """
    try:
        turn.steps = frames
        turn.tokens = int((frames[-1].get("token") or {}).get("total", 0)) if frames else 0
        await turn.save(update_fields=["steps", "tokens"])
        if turn.tokens:
            # 会话累计：读出当前值加上本轮（会话对象就在手里，不必再查一次）
            session.tokens = (session.tokens or 0) + turn.tokens
            await session.save(update_fields=["tokens"])
        logger.info(f"save_steps: turn {turn.id} 存了 {len(frames)} 帧，Token {turn.tokens}"
                    f"（会话累计 {session.tokens}）")
    except Exception as error:  # noqa: BLE001 - 进度与用量是附加记录，写不进去不该影响检索结果
        logger.warning(f"save_steps: 保存轮次 {turn.id} 的进度失败：{error}")


async def output(content: str, database_id: int, session: Session,
                 turn: Turn) -> AsyncGenerator[str, Any]:
    """把检索进度包成 SSE 数据帧：节点每上报一条 Step，就推一帧 `data: {json}\\n\\n`。

    :param content: 本轮的输入（恢复时是对上一个反问的回答）
    :param database_id: 本次检索的业务库编号
    :param session: 这一轮所属的会话（见 resolve_session）：thread_id 由它取，session_id 也随每帧带回客户端
    :param turn: 这一轮的登记（见 start_turn）：进度随帧累积，整轮结束时写回

    每帧都带上 session_id（Step.session_id）：客户端据此把下一轮接回同一个会话，不必自己记 thread_id；
    每帧也带上「到目前为止」的累计 Token 用量（Step.token），前端可以直接显示花了多少。
    进度按推给客户端的同一份 JSON 留档（dump 一次既推帧也存库，见 plugins.json）：之后要看「这一轮跑到哪、
    拿到了什么」，直接读 Turn.steps 即可；Token 用量另记在 Turn.tokens 上，并累加到 Session.tokens。
    """
    frames: list[dict[str, Any]] = []
    try:
        async for step in retriever.invoke(content, database_id, str(session.thread_id)):
            step.session_id = session.id
            frame = load(dump(asdict(step), default=str))
            frames.append(frame)
            yield f"data: {dump(frame)}\n\n"
    finally:
        # 放在 finally：正常跑完、节点抛错、在反问处停下都留档（客户端中途断开时尽力而为，取决于取消时机）
        await save_steps(session, turn, frames)


@router.post("")
async def submit_question(question: Question):
    """把自然语言问题转成 SQL 并执行：逐条产出检索进度（SSE 数据帧，与采集流程的输出格式一致）。

    content 为待查询的自然语言问题；database_id 指定本次检索的业务库编号（元数据库里的 Database.id），
    字段、取值与指标都只在该库的向量集合里召回，不跨库。问题不适合查询、SQL 校验未通过或执行报错，
    都以携带 error 的错误帧结束，不抛出异常。

    session_id 用于续跑（不带即开一轮新的对话）：会话到 thread_id 的对应由服务端查，客户端只认 session_id。
    链路在指标口径对不上时会停下来反问（帧里带 clarification），客户端把用户的回答作为 content、带上同一个
    session_id 再提交一次，即从停住的地方继续（见 plugins.agents.graph.Retriever.invoke）。新建的会话编号
    在响应头 X-Session-Id 与每帧的 session_id 里带回来（见 resolve_session / output）。

    会话与轮次同时落库（见 start_turn / save_steps）：一轮提问就是一条 Turn，记下用户输入与这一轮的
    全部进度帧；同一个 session_id 的多轮挂在同一条 Session 下（见 models.session，轮次序号按提交顺序递增）。
    进度在整轮结束时写：中途停下等反问、报错，已经跑出来的帧照样留档。
    """
    session = await resolve_session(question.session_id)
    turn = await start_turn(session, question.content)
    logger.info(f"submit_question: session {session.id}（thread {session.thread_id}）, "
                f"database {question.database_id}, turn {turn.id}（第 {turn.round} 轮）")
    return StreamingResponse(
        output(question.content, question.database_id, session, turn),
        media_type="text/event-stream",
        # 会话编号先以响应头给出：客户端在读第一帧之前就能拿到，下一轮直接带上
        headers={"X-Session-Id": str(session.id)}
    )


@router.get("/session")
async def list_sessions(limit: int = Query(default=100, ge=1, le=500)):
    """列出会话：按编号倒序（新的在前），每条带上轮次数与最近一轮时间。

    :param limit: 最多返回多少条（默认 100，上限 500）：会话只增不减，列表按需截断，要看更早就把 limit 调大

    返回的 id 与 thread_id 都是雪花 ID：id 是本系统的会话编号（提问与删除会话都按它定位），thread_id 是检索
    那边的会话编号（客户端不必关心，列出来便于排查）；turns 是这条会话下的轮次数，last_turn_at 是最近一轮的
    创建时间（还没有轮次时为空）。轮次的明细（用户输入、进度帧）体积大，不在列表里——它们是另一份记录，
    列表只回答「有哪些会话、各聊了几轮」。
    """
    rows = await Session.annotate(turns=Count("rounds"), last_turn_at=Max("rounds__created_at")) \
        .order_by("-id").limit(limit) \
        .values("id", "thread_id", "created_at", "turns", "last_turn_at")
    return list(rows)


@router.get("/session/{session_id}")
async def get_session(session_id: int):
    """查看会话详情：会话本身的字段 + 它名下的全部轮次（按轮次序号升序）。

    :param session_id: 会话编号（Session.id，提交问题时由响应头 / 帧带回来）；不存在时报 404

    与列表接口（见 list_sessions）同源，多了 turns：每一轮的用户输入、进度帧、Token 用量与创建时间。
    列表接口不带轮次明细（帧里可能带着整张表的字段与取值，体积很大），要看「每一轮问了什么、跑到哪、
    生成了什么 SQL」就用这个接口。

    轮次按 round 升序——同一会话从 1 开始递增，顺序即提问顺序；正在跑的那一轮也在里面（steps 是已推给
    客户端的那部分帧，整轮结束时才整体写回，见 save_steps），所以最后一轮的 steps 可能不全。会话的 tokens
    是各轮之和（见 Session.tokens），可与 turns 里的用量核对。
    """
    session = await Session.get_or_none(id=session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的会话不存在")
    turns = await Turn.filter(session_id=session_id).order_by("round")
    return {
        "id": session.id,
        "thread_id": session.thread_id,
        "created_at": session.created_at,
        "tokens": session.tokens,
        "turns": [
            {
                "id": turn.id,
                "round": turn.round,
                "created_at": turn.created_at,
                "input": turn.input,
                "tokens": turn.tokens,
                "steps": turn.steps or []
            }
            for turn in turns
        ]
    }


@router.delete("/session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int):
    """删除会话：先清掉这一轮会话的运行状态（记忆），确认清干净了再删会话与它名下的轮次。

    session_id 是会话编号（Session.id，提交问题时由响应头 / 帧带回来）；会话不存在时报 404。
    thread_id 不必由客户端提供：会话登记里存着它（见 models.session.Session.thread_id），这里查出来再清记忆。

    顺序是「先清记忆、后删登记」，两步都成功才算删掉：
    1. 按会话的 thread_id 清掉检索智能体的记忆（见 Retriever.forget）：记忆是续跑的前提，清不干净就直接以
       503 结束、登记原样保留（客户端可以重试）——反过来先删登记的话，那个 thread_id 的旧状态还在，
       下次提问会被当成「停住的那一轮的恢复」，一上来就接到旧状态上，且再没有登记可据以重清；
    2. 记忆清干净后再删会话（Turn 随外键 CASCADE 一起删）。这一步失败只报错、不补做记忆清理：
       记忆已经没了，剩下的是一条没有记忆的会话登记，重试本接口即可（再清一次记忆是无害的空操作）。
    """
    session = await Session.get_or_none(id=session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的会话不存在")
    thread = str(session.thread_id)
    try:
        await retriever.forget(thread)
    except Exception as error:  # noqa: BLE001 - 记忆清不干净就不删登记，让调用方重试
        logger.warning(f"delete_session: 清理会话 {session_id}（thread {thread}）的记忆失败，未删除登记：{error}")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "清理会话记忆失败，会话未删除，请稍后重试")
    await session.delete()
    logger.info(f"delete_session: session {session_id}（thread {thread}）deleted")
