# nextgen_plugins/chatbox/validators.py
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DATE_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$")
VPU_PREFIX_RE = re.compile(r"^VPU[_\s-]*(\d{1,2})([A-Za-z]?)$", re.IGNORECASE)
VPU_NUM_RE = re.compile(r"^(\d{1,2})([A-Za-z]?)$", re.IGNORECASE)
VPU_ALLOWED_SUFFIXES = {"U", "L", "W", "S", "N"}

Forecasts = Literal["short_range", "medium_range", "analysis_assim_extend"]


def normalize_date_ymd(s: str) -> str:
    s = (s or "").strip().replace("/", "-")
    datetime.strptime(s, "%Y-%m-%d")
    return s


def normalize_vpu(s: str) -> str:
    """
    Normalize VPU identifiers to canonical ids.

    Accepted examples:
      - VPU_06
      - VPU 6
      - VPU6
      - 6
      - VPU_03W
      - VPU 3W
      - VPU3W
      - 3W
      - 10u -> VPU_10U
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("vpu is required")

    m = VPU_PREFIX_RE.match(raw) or VPU_NUM_RE.match(raw)
    if not m:
        raise ValueError(
            "vpu must look like VPU_02, VPU 2, 2, VPU_03W, VPU 3W, or 3W"
        )

    num = int(m.group(1))
    suffix = (m.group(2) or "").upper()

    if suffix and suffix not in VPU_ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(VPU_ALLOWED_SUFFIXES))
        raise ValueError(f"unsupported VPU suffix '{suffix}'. Allowed suffixes: {allowed}")

    return f"VPU_{num:02d}{suffix}"


def normalize_cycle_hour(s: str) -> str:
    raw = (s or "").strip()
    if raw.isdigit() and len(raw) in (1, 2):
        hh = int(raw)
        if 0 <= hh <= 23:
            return f"{hh:02d}"
    raise ValueError("cycle must be an hour 00–23 (two digits preferred)")


class OutputsFilesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1, description="Model id (e.g., cfe_nom)")
    date: str = Field(description="Date in YYYY-MM-DD or YYYY/MM/DD")
    forecast: Forecasts = Field(description="Forecast id")
    cycle: str = Field(description="Cycle hour (00–23). Forecast-specific allowed values.")
    vpu: str = Field(
        description="VPU id or label (e.g., VPU_02, VPU 2, 2, VPU_03W, VPU 3W, 3W)."
    )
    ensemble: Optional[int] = Field(
        default=None,
        ge=1,
        description="Only for medium_range. Defaults to 1 (first member) if omitted.",
    )

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_and_validate_date(cls, v):
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        if not isinstance(v, str) or not DATE_RE.match(v.strip()):
            raise ValueError("date must be YYYY-MM-DD or YYYY/MM/DD")
        return normalize_date_ymd(v)

    @field_validator("vpu")
    @classmethod
    def _validate_vpu(cls, v: str) -> str:
        return normalize_vpu(v)

    @field_validator("cycle")
    @classmethod
    def _validate_cycle(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("cycle is required")
        return normalize_cycle_hour(v)

    @model_validator(mode="after")
    def _forecast_dependent_rules(self) -> "OutputsFilesQuery":
        if self.forecast == "short_range":
            pass

        elif self.forecast == "medium_range":
            if self.cycle not in {"00", "06", "12", "18"}:
                raise ValueError("For medium_range, cycle must be one of: 00, 06, 12, 18")
            if self.ensemble is None:
                self.ensemble = 1

        elif self.forecast == "analysis_assim_extend":
            if self.cycle != "16":
                raise ValueError("For analysis_assim_extend, cycle must be: 16")
            if self.ensemble is not None:
                raise ValueError("ensemble is not valid for analysis_assim_extend")

        if self.forecast != "medium_range" and self.ensemble is not None:
            raise ValueError("ensemble is only valid for medium_range")

        return self