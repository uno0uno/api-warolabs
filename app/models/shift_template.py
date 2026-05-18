"""Pydantic models for tenant shift templates (warocol.com#682)."""
from datetime import time
from typing import Optional

from pydantic import BaseModel, Field, model_validator


def _validate_shift_window(
    *,
    start_time: time,
    end_time: time,
    crosses_midnight: bool,
) -> None:
    if not crosses_midnight and end_time <= start_time:
        raise ValueError(
            "end_time must be after start_time when crosses_midnight is false"
        )


class ShiftTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    start_time: time
    end_time: time
    crosses_midnight: bool = False
    sort_order: int = Field(0, ge=0, le=32767)

    @model_validator(mode="after")
    def _validate_window(self):
        _validate_shift_window(
            start_time=self.start_time,
            end_time=self.end_time,
            crosses_midnight=self.crosses_midnight,
        )
        return self


class ShiftTemplatePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    crosses_midnight: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0, le=32767)
    is_active: Optional[bool] = None
