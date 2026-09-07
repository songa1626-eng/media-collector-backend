from __future__ import annotations

import enum
from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class MediaType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class TargetInfo(BaseModel):
    provider: str
    target_id: str
    display_name: str = ""
    avatar_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class MediaItem(BaseModel):
    url: str
    media_type: MediaType = MediaType.IMAGE
    filename: str = ""
    source_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class TaskCreate(BaseModel):
    provider: str
    target_url: str
    save_dir: str = ""
    start_page: int = 1
    end_page: int = 0
    start_date: date | None = None
    end_date: date | None = None
    full_history: bool = False


class TargetResolveRequest(BaseModel):
    target_url: str


class TargetUrlsRequest(BaseModel):
    target_urls: list[str] = Field(default_factory=list)


class TaskInfo(BaseModel):
    id: int = 0
    provider: str
    target_url: str
    target_name: str = ""
    save_dir: str = ""
    status: TaskStatus = TaskStatus.PENDING
    total_count: int = 0
    downloaded_count: int = 0
    failed_count: int = 0
    filtered_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    start_page: int = 1
    end_page: int = 0
    start_date: date | None = None
    end_date: date | None = None
    full_history: bool = False
    next_page: int = 1
    fetch_complete: bool = False
    error_message: str = ""
    progress_message: str = ""


class ProviderInfo(BaseModel):
    name: str
    display_name: str
    description: str = ""
    config_schema: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ProviderConfigUpdate(BaseModel):
    provider: str
    config: dict[str, Any]
