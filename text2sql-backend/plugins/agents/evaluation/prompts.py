from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


QUESTION_GENERATION_PROMPT = _load("question_generation.md")

SQL_GENERATION_PROMPT = _load("sql_generation.md")

SQL_CORRECTION_PROMPT = _load("sql_correction.md")

EVALUATION_REVIEW_PROMPT = _load("evaluation_review.md")
