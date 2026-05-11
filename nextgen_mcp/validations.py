# validations.py
from typing import Literal

FORECASTS = Literal["short_range", "medium_range", "analysis_assim_extend"]
MODELS = Literal["cfe_nom", "lstm", "routing_only"]

# ---------------------------------------------------------------------------
# LLM-facing hint strings for @mcp.tool / @mcp.prompt argument descriptions.
#
# Centralized here (alongside the Literal type definitions they describe) so
# the prose stays in lockstep with the value space. The mcp_server.py
# argument annotations import and reuse these constants instead of inlining
# the strings, which previously produced 30+ duplicated literals across the
# tool surface — the LOCKSTEP-RULE drift risk documented in the prompts.
# ---------------------------------------------------------------------------
MODEL_HINT = "cfe_nom / lstm / routing_only"
FORECAST_HINT = "short_range / medium_range / analysis_assim_extend"
DATE_HINT = "yyyy-mm-dd"
CYCLE_HINT = "00-23, e.g., 00"
VPU_HINT = "06, VPU_06, or 3W"