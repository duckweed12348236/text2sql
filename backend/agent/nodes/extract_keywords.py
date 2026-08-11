from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
import jieba.analyse


async def extract_keywords(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="提取关键字", status="running"))

    query = state.messages[-1].content
    # 对查询进行分词，只提取指定词性的词
    allow_pos = (
        "n",  # 名词: 数据、服务器、表格
        "nr",  # 人名: 张三、李四
        "ns",  # 地名: 北京、上海
        "nt",  # 机构团体名: 政府、学校、某公司
        "nz",  # 其他专有名词: Unicode、哈希算法、诺贝尔奖
        "v",  # 动词: 运行、开发
        "vn",  # 名动词: 工作、研究
        "a",  # 形容词: 美丽、快速
        "an",  # 名形词: 难度、合法性、复杂度
        "eng",  # 英文
        "i",  # 成语
        "l",  # 常用固定短语
        "m", # 数量词
        "t", # 时间
    )
    keywords = jieba.analyse.extract_tags(query, allowPOS=allow_pos)
    keywords = list(set(keywords + [query]))
    writer(WSStepInfo(step="提取关键字", status="success"))
    return {"keywords": keywords}