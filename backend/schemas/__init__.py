from typing import Any
from pydantic import BaseModel, ConfigDict, TypeAdapter


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


str_adapter = TypeAdapter(str)
str_list_adapter = TypeAdapter(list[str])
str_key_any_value_dict_adapter = TypeAdapter(dict[str, Any])
