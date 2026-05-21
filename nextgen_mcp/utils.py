import re
from typing import Dict, Any, Optional
from datetime import datetime, date
from zoneinfo import ZoneInfo

from ._mcp import LOGGER
from .validation import normalize_vpu
from .middleware._input_validation_middleware import InvalidLLMInputError

DEFAULT_START = "2025-08-01"
DEFAULT_TZ = ZoneInfo("America/Denver")


def _as_id(value: str) -> str:
    """
    Convert user-facing labels to canonical ids for known patterns:
      - forecasts: "short range" -> "short_range"
      - vpu: "VPU 14" -> "VPU_14"
      - vpu subregions: "VPU 3W" -> "VPU_03W", "10u" -> "VPU_10U"

    If the value is already canonical, it is returned unchanged.
    """
    if value is None:
        return value

    s = str(value).strip()
    if not s:
        return s

    forecast_candidate = s.lower().replace(" ", "_")
    if forecast_candidate in {"short_range", "medium_range", "analysis_assim_extend"}:
        return forecast_candidate

    try:
        return normalize_vpu(s)
    except ValueError:
        pass

    return s.replace(" ", "_")

def _prefer_id_objects(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    """
    Normalize payload[key] into a list of {id, label} objects and always
    populate companion *_ids and *_labels arrays.

    Rules:
      - list[{"id": ..., "label": ...}] -> preserved
      - list[{"name": ..., "path": ...}] -> id/label default to name
      - list[str] -> [{"id": s, "label": s}]
      - missing/empty/non-list -> empty normalized list
    """
    singular = key[:-1] if key.endswith("s") else key
    ids_key = f"{singular}_ids"
    labels_key = f"{singular}_labels"

    items = payload.get(key)
    normalized: list[dict[str, Any]] = []

    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                item_id = item.get("id")
                if item_id is None:
                    item_id = item.get("name") or item.get("path")

                if item_id is None:
                    continue

                label = item.get("label")
                if label is None:
                    label = item.get("name") or str(item_id)

                obj = dict(item)
                obj["id"] = str(item_id)
                obj["label"] = str(label)
                normalized.append(obj)
            else:
                text = str(item)
                normalized.append({"id": text, "label": text})

    payload[key] = normalized
    payload[ids_key] = [x["id"] for x in normalized]
    payload[labels_key] = [x["label"] for x in normalized]
    return payload

def _parse_iso_date(s: str) -> date:
    s = s.strip().replace("/", "-")
    return datetime.strptime(s, "%Y-%m-%d").date()

def _date_from_item(d: dict) -> Optional[date]:
    """
    Accepts items shaped like:
      {id:"ngen.YYYYMMDD", label:"YYYY-MM-DD"} or similar.
    """
    label = str(d.get("label") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", label):
        try:
            return _parse_iso_date(label)
        except Exception:
            return None

    did = str(d.get("id") or "")
    m = re.search(r"(\d{8})", did)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
        except Exception:
            return None

    return None


_MIN_ALLOWED_DATE = _parse_iso_date(DEFAULT_START)


def _preview_text(value: Optional[str], limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def _summarize_tool_result(result) -> str:
    """One-line log summary for a tool result envelope.

    Reports shape and a small bounded sample — never dumps the full
    `data` payload. Use this in place of
    ``LOGGER.info("...result: %s", result)`` for any tool whose `data`
    can grow unbounded (per-row dumps overwhelm server logs and bury
    actionable signal). Output is single-line and capped at ~500 chars
    regardless of payload size.

    Success example::

        ok=True file_count=10 rows=240 columns=[time,flow] sample_row={'time': '2026-05-18T02:00:00.000000Z', 'flow': 18.58}

    Error example::

        ok=False code=not_found message=No output files matched. fix_hint_preview=...

    Tolerates non-dict and missing-field results so the log call never
    raises and never partially-suppresses a tool result the caller still
    needs to return upstream.
    """
    if not isinstance(result, dict):
        return f"non_dict_result type={type(result).__name__}"

    if result.get("ok") is False:
        err = result.get("error") or {}
        code = message = None
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message")
        bits = [f"ok=False code={code or 'unknown'}"]
        if message:
            bits.append(f"message={_preview_text(str(message), 120)}")
        fix_hint = result.get("fix_hint")
        if fix_hint:
            bits.append(f"fix_hint_preview={_preview_text(str(fix_hint), 120)}")
        return " ".join(bits)

    bits = ["ok=True"]
    if "file_count" in result:
        bits.append(f"file_count={result['file_count']}")
    if "rows" in result:
        bits.append(f"rows={result['rows']}")
    columns = result.get("columns")
    if isinstance(columns, list):
        # Surface the actual column names (truncated) so the operator
        # can verify the projection without the data array.
        col_list = ",".join(str(c) for c in columns[:8])
        if len(columns) > 8:
            col_list += f",...+{len(columns) - 8}"
        bits.append(f"columns=[{col_list}]")
    # A single sample row from `data` lets the operator spot-check that
    # the values look right. Truncate to a bounded length so wide rows
    # never blow up the log line.
    data = result.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        bits.append(f"sample_row={_preview_text(str(first), 240)}")
    return " ".join(bits)


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


_NULL_LITERALS = frozenset(("none", "null", "nil", "<nil>", "undefined", ""))


def _coerce_none_string(v):
    """Coerce LLM-emitted null-literals to actual ``None``.

    Workshop-class small models (observed: nemotron-3-nano:30b 2026-05-20)
    sometimes emit string literals like ``"<nil>"`` / ``"None"`` / ``"null"``
    when they want to pass ``None`` to an ``Optional[str]`` arg. Pydantic's
    ``str`` pattern matcher rejects these because the literal doesn't match
    the field's regex; the validator-envelope middleware then surfaces an
    ``invalid_args:`` envelope and the LLM retries with actual ``None``.

    This helper short-circuits that recovery round-trip by stripping common
    null-literals to ``None`` at the ``BeforeValidator`` stage - before the
    pattern check fires. Apply via:

        Annotated[Optional[str], BeforeValidator(_coerce_none_string),
                 Field(..., pattern=...)]

    Recovery via the validator envelope still works for null-literals not in
    this allowlist (small models invent novel-looking nulls); this just makes
    the common case cheaper.
    """
    if isinstance(v, str) and v.strip().lower() in _NULL_LITERALS:
        return None
    return v


def _require(**kwargs):
    """Validate that required parameters are not None. Returns error dict or None."""
    missing = [k for k, v in kwargs.items() if v is None]
    if missing:
        discovery = {
            "model": "list_available_models",
            "forecast": "list_available_forecasts",
            "vpu": "list_available_vpus",
            "query": "the query is required",
        }
        hints = [f"{k} (use {discovery.get(k, 'discovery')})" for k in missing]
        return {
            "error": f"Missing required parameters: {', '.join(hints)}. "
            "Call the appropriate discovery tool first."
        }
    return None
