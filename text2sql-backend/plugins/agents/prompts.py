"""全部提示词：按用途分节，正文放在同目录的 prompts/*.md 里。

三张图（检索 / 采集 / 评估）的提示词都在这一份里，改文案只动 md 文件、名字与用途在这里对应；
加载即读盘，模块导入时一次性读入，热改文案需重启进程。
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


# ── 检索：意图、关键词扩展、表与指标的筛选与重排 ──────────────────────────────────────────────

INTENTION_ANALYSIS_PROMPT = _load("intention_analysis.md")

# 意图识别之后、关键词提取之前：把口语化的问题改写完整（补实体类型、补要看的属性）
QUESTION_REWRITE_PROMPT = _load("question_rewrite.md")

COLUMN_KEYWORD_EXTENSION_PROMPT = _load("column_keyword_extension.md")

# 生成 SQL 时发现候选表里缺东西：产出新的问题与关键词，用于再召回一轮
RECALL_PROBE_PROMPT = _load("recall_probe.md")

METRIC_KEYWORD_EXTENSION_PROMPT = _load("metric_keyword_extension.md")

TABLE_FILTERING_PROMPT = _load("table_filtering.md")

RELATION_RECOGNITION_PROMPT = _load("relation_recognition.md")

METRIC_MATCHING_PROMPT = _load("metric_matching.md")

METRIC_ANSWER_PROMPT = _load("metric_answer_validation.md")

# 重排指令（rerank 的 instruct 参数）：说明文档是什么、按什么标准判断相关性
TABLE_RERANK_INSTRUCT = _load("table_rerank.md")

METRIC_RERANK_INSTRUCT = _load("metric_rerank.md")

SQL_GENERATION_PROMPT = _load("sql_generation.md")

SQL_CORRECTION_PROMPT = _load("sql_correction.md")

SQL_VALIDATION_PROMPT = _load("sql_validation.md")

# ── 采集：按表结构分析出角色、描述、别名与同步标记 ───────────────────────────────────────────

TABLE_DISCOVER_PROMPT = _load("table_discover.md")

# ── 评估：出题、批量生成 / 纠正参考 SQL、评测解读 ────────────────────────────────────────────
# 评估侧的 SQL 提示词与检索侧同名的两份不是一回事（面对的是「一批问题」而不是「一轮检索」），故文件带
# evaluation_ 前缀，避免与上面的 SQL_GENERATION_PROMPT / SQL_CORRECTION_PROMPT 混用。

QUESTION_GENERATION_PROMPT = _load("evaluation_question_generation.md")

EVALUATION_SQL_GENERATION_PROMPT = _load("evaluation_sql_generation.md")

EVALUATION_SQL_CORRECTION_PROMPT = _load("evaluation_sql_correction.md")

EVALUATION_REVIEW_PROMPT = _load("evaluation_review.md")
