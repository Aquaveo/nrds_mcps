# validations.py
from typing import Literal

FORECASTS = Literal["short_range", "medium_range", "analysis_assim_extend"]
MODELS = Literal["cfe_nom", "lstm", "routing_only"]