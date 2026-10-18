from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

TABLE_DISCOVER_PROMPT = (PROMPTS_DIR / "table_discover.md").read_text(encoding="utf-8")
