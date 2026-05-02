# utils.py
import requests
import os
import re
from typing import Dict, Any, Optional
from datetime import datetime, date
from zoneinfo import ZoneInfo
from .validators import normalize_vpu
from .rest import (
    list_available_models,
    list_available_dates,
    list_available_forecasts,
    list_available_cycles,
    list_available_vpus,
    list_available_output_files,
    get_output_file,
    query_output_file,
    query_output_file_from_output_selector,
    create_plotly_chart_from_output_selector,
    query_hydrofabric_parquet_file,
    build_hydrofabric_feature_map_config,
    create_plotly_chart_from_output_file,
)

REST_API_HOST = os.getenv("NRDS_API_HOST", "http://localhost:8000/apps/nrds/api").rstrip("/")
DATE_PATTERN = r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})$"
DEFAULT_START = "2025-08-01"
DEFAULT_TZ = ZoneInfo("America/Denver")

def _get_json_raw(endpoint_key: str, params: Optional[Dict[str, Any]] = None, **_) -> Dict[str, Any]:
    p = params or {}

    if endpoint_key == "list_available_models":
        return list_available_models()

    if endpoint_key == "list_available_dates":
        return list_available_dates(model=p["model"])

    if endpoint_key == "list_available_forecasts":
        return list_available_forecasts(model=p["model"], date=p["date"])

    if endpoint_key == "list_available_cycles":
        return list_available_cycles(model=p["model"], date=p["date"], forecast=p["forecast"])

    if endpoint_key == "list_available_vpus":
        return list_available_vpus(model=p["model"], date=p["date"], forecast=p["forecast"], cycle=p["cycle"])

    if endpoint_key == "list_available_output_files":
        return list_available_output_files(data=p)

    if endpoint_key == "get_output_file":
        return get_output_file(
            model=p["model"], date=p["date"], forecast=p["forecast"], cycle=p["cycle"], vpu=p["vpu"],
            file_name=p.get("file_name"), index=p.get("index"), ensemble=p.get("ensemble")
        )
    
    if endpoint_key == "query_output_file":
        return query_output_file(s3_url=p["s3_url"], query=p["query"])
    
    if endpoint_key == "query_output_file_from_output_selector":
        return query_output_file_from_output_selector(
            model=p["model"],
            date=p["date"],
            forecast=p["forecast"],
            cycle=p["cycle"],
            vpu=p["vpu"],
            query=p["query"],
            ensemble=p.get("ensemble"),
            file_name=p.get("file_name"),
            index=p.get("index"),
        )

    if endpoint_key == "query_hydrofabric_parquet_file":
        return query_hydrofabric_parquet_file(
            hydrofabric_id=p["hydrofabric_id"],
            limit=p["limit"]
        )
    
    if endpoint_key == "create_plotly_chart_from_output_file":
        return create_plotly_chart_from_output_file(
            s3_url=p["s3_url"],
            query=p["query"],
            title=p.get("title"),
        )
    
    if endpoint_key == "build_hydrofabric_feature_map_config":
        return build_hydrofabric_feature_map_config(
            hydrofabric_id=p["hydrofabric_id"], 
        )
    
    if endpoint_key == "create_plotly_chart_from_output_selector":
        return create_plotly_chart_from_output_selector(
            model=p["model"],
            date=p["date"],
            forecast=p["forecast"],
            cycle=p["cycle"],
            vpu=p["vpu"],
            query=p["query"],
            title=p.get("title"),
            ensemble=p.get("ensemble"),
            file_name=p.get("file_name"),
            index=p.get("index"),
        )

    raise KeyError(f"Unknown endpoint_key: {endpoint_key}")


def _is_html_response(resp: requests.Response) -> bool:
    """
        Heuristic to determine if a response is HTML (e.g., an error page) rather than JSON.
        Checks Content-Type header and also looks for HTML tags in the text.
    """
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ctype:
        return True
    # some servers mislabel html; quick heuristic
    text = (resp.text or "").lstrip()
    return text.startswith("<!DOCTYPE html") or text.startswith("<html")

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
