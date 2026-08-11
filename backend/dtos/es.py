from pydantic import BaseModel


class ValueInfo(BaseModel):
    id: str
    value: str
    column_id: str