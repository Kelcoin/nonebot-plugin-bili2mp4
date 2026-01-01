from typing import Optional

from pydantic import BaseModel, Field


class Config(BaseModel):
    super_admins: list[int] = Field(default=[])
    ffmpeg_path: Optional[str] = Field(
        default=None,
        description="FFmpeg可执行文件所在目录路径，不是ffmpeg文件本身的路径",
    )
