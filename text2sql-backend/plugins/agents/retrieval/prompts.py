from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


INTENTION_ANALYSIS_PROMPT = _load("intention_analysis.md")

COLUMN_KEYWORD_EXTENSION_PROMPT = _load("column_keyword_extension.md")

METRIC_KEYWORD_EXTENSION_PROMPT = _load("metric_keyword_extension.md")

VALUE_KEYWORD_EXTENSION_PROMPT = _load("value_keyword_extension.md")

TABLE_FILTERING_PROMPT = _load("table_filtering.md")

METRIC_FILTERING_PROMPT = _load("metric_filtering.md")

# 重排指令（rerank 的 instruct 参数）：说明文档是什么、按什么标准判断相关性
TABLE_RERANK_INSTRUCT = _load("table_rerank.md")

METRIC_RERANK_INSTRUCT = _load("metric_rerank.md")

SQL_GENERATION_PROMPT = _load("sql_generation.md")

SQL_CORRECTION_PROMPT = _load("sql_correction.md")

SQL_VALIDATION_PROMPT = _load("sql_validation.md")
