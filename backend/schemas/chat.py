from pydantic import Field

from schemas import Schema


class Question(Schema):
    content: str = Field(min_length=1)
