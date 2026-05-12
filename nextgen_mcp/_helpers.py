from _mcp import LOGGER
from typing import Optional
from datetime import datetime
from ._input_validation_middleware import InvalidLLMInputError

from .utils import (
    _parse_iso_date,
    DEFAULT_TZ,
    DEFAULT_START,
)

_MIN_ALLOWED_DATE = _parse_iso_date(DEFAULT_START)


def _preview_text(value: Optional[str], limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."

def _validate_date_bounds(d, field_name: str):
    today = datetime.now(DEFAULT_TZ).date()
    LOGGER.debug(
        "Validating date bounds for field=%s value=%s allowed_range=[%s, %s]",
        field_name,
        d,
        _MIN_ALLOWED_DATE,
        today,
    )
    if d < _MIN_ALLOWED_DATE or d > today:
        LOGGER.warning(
            "Date validation failed for field=%s value=%s allowed_range=[%s, %s]",
            field_name,
            d,
            _MIN_ALLOWED_DATE,
            today,
        )
        raise InvalidLLMInputError(
            f"'{field_name}' must be between {_MIN_ALLOWED_DATE} and {today} (got {d})"
        )
    return d


def _parse_date_or_today(date_str: Optional[str], field_name: str):
    LOGGER.debug("Parsing date for field=%s raw_value=%s", field_name, date_str)
    d = (
        _parse_iso_date(date_str)
        if date_str is not None
        else datetime.now(DEFAULT_TZ).date()
    )
    validated = _validate_date_bounds(d, field_name)
    LOGGER.debug("Parsed date for field=%s resolved_value=%s", field_name, validated)
    return validated


def _require(**kwargs):
    """Validate that required parameters are not None. Returns error dict or None."""
    missing = [k for k, v in kwargs.items() if v is None]
    if missing:
        discovery = {"model": "list_available_models", "forecast": "list_available_forecasts", "vpu": "list_available_vpus", "query": "the query is required"}
        hints = [f"{k} (use {discovery.get(k, 'discovery')})" for k in missing]
        return {"error": f"Missing required parameters: {', '.join(hints)}. Call the appropriate discovery tool first."}
    return None