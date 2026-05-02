# validations.py
from typing import Literal

FORECASTS = Literal["short_range", "medium_range", "analysis_assim_extend"]
VPUS = Literal[
    "VPU_18",
    "VPU_16",
    "VPU_15",
    "VPU_14",
    "VPU_13",
    "VPU_12",
    "VPU_11",
    "VPU_10U",
    "VPU_10L",
    "VPU_09",
    "VPU_08",
    "VPU_07",
    "VPU_06",
    "VPU_05",
    "VPU_04",
    "VPU_03W",
    "VPU_03S",
    "VPU_03N",
    "VPU_02",
    "VPU_01",
]
MODELS = Literal["cfe_nom", "lstm", "routing_only"]
MEDIUM_RANGE_CYCLES = Literal["00", "06", "12", "18"]
SHORT_RANGE_CYCLES = Literal["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22", "23"]
ANALYSIS_ASSIM_EXTEND_CYCLES = Literal["16"]