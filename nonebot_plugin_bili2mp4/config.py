from pydantic import BaseModel, Field


class Config(BaseModel):
    super_admins: list[int] = Field(default=[])
