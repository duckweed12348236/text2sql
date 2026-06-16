from langchain_openai import ChatOpenAI

from config import DEEPSEEK_MODEL_NAME, DEEPSEEK_MODEL_URL, DEEPSEEK_MODEL_KEY

deepseek_model = ChatOpenAI(
    api_key=DEEPSEEK_MODEL_KEY,
    model=DEEPSEEK_MODEL_NAME,
    base_url=DEEPSEEK_MODEL_URL,
    temperature=0
)
