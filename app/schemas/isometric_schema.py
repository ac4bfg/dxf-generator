from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional, Tuple, Union


StartVariant = Literal["start-BR", "start-BL", "start-TR", "start-TL"]
Direction = Literal["forward", "back", "right", "left", "up", "down"]
DimSide = Literal["default", "opposite", "right", "left", "top", "bottom"]


class PipeSegment(BaseModel):
    type: Literal["pipe"] = "pipe"
    length_mm: float = Field(..., gt=0, description="Pipe length in millimeters")
    direction: Optional[Direction] = Field(None, description="Direction keyword (auto-translated per variant)")
    direction_by_variant: Optional[Dict[StartVariant, Direction]] = None
    angle: Optional[float] = Field(None, ge=0, lt=360, description="Absolute angle (if no direction)")
    angle_by_variant: Optional[Dict[StartVariant, float]] = None
    no_transform: bool = False
    dimension: bool = False
    dimension_side: Optional[DimSide] = "default"
    bend_side: Optional[Literal["left", "right"]] = None
    bend_side_by_variant: Optional[Dict[StartVariant, Literal["left", "right"]]] = None

    class Config:
        extra = "ignore"


class ComponentSegment(BaseModel):
    type: Literal["component"] = "component"
    block: str = Field(..., description="Block name in template")
    block_by_variant: Optional[Dict[StartVariant, str]] = None
    color: Optional[int] = Field(None, ge=0, le=256)
    gap: Optional[float] = Field(None, ge=0, description="Gap length in drawing units (auto-calc if None)")
    scale: Optional[List[float]] = Field(None, min_length=2, max_length=2)
    scale_by_variant: Optional[Dict[StartVariant, List[float]]] = None
    auto_mirror: bool = True
    rotation: Optional[float] = None
    rotation_by_variant: Optional[Dict[StartVariant, float]] = None
    direction: Optional[Direction] = None
    direction_by_variant: Optional[Dict[StartVariant, Direction]] = None
    canonical_direction_angle: float = 0
    insert_offset_by_variant: Optional[Dict[StartVariant, List[float]]] = None
    bend_side: Optional[Literal["left", "right"]] = None
    bend_side_by_variant: Optional[Dict[StartVariant, Literal["left", "right"]]] = None
    dimension: bool = False
    dimension_side: Optional[DimSide] = "default"

    class Config:
        extra = "ignore"


Segment = Union[PipeSegment, ComponentSegment]


class CombinedDim(BaseModel):
    from_seg: int = Field(..., ge=0)
    to_seg: int = Field(..., ge=0)
    text_mm: Optional[float] = None
    side: Optional[DimSide] = "default"

    class Config:
        extra = "ignore"


class IsometricGenerateRequest(BaseModel):
    module: Literal["SR", "SK"] = "SR"
    start_block: Optional[str] = Field("start-BR", description="Start block variant. None/omit for SK (no anchor).")
    start_insert: Optional[Tuple[float, float]] = Field(
        None, description="Insert position. If None, auto-use variant default.")
    start_rotation: float = 0
    segments: List[Segment] = Field(..., min_length=1)
    combined_dims: List[CombinedDim] = []
    output_format: Literal["dxf", "dwg", "pdf"] = "dwg"
    file_name: Optional[str] = Field(None, description="Output filename (without extension)")
    customer_data: Optional[Dict[str, Any]] = Field(None, description="Customer data for text replacement in template")

    class Config:
        json_schema_extra = {
            "example": {
                "start_block": "start-BR",
                "start_insert": [209.52, 124.98],
                "segments": [
                    {"type": "pipe", "direction": "forward", "length_mm": 5000, "dimension": True},
                    {"type": "pipe", "direction": "up", "length_mm": 500},
                    {"type": "component", "block": "klem_3-4", "color": 5, "gap": 3.07},
                    {"type": "pipe", "direction": "up", "length_mm": 2000},
                    {"type": "component", "block": "ball_valve_3-4", "color": 5, "gap": 4.0},
                    {"type": "pipe", "direction": "up", "length_mm": 500},
                    {"type": "pipe", "direction": "right", "length_mm": 250,
                     "direction_by_variant": {"start-TR": "left"},
                     "bend_side_by_variant": {"start-BL": "right"}},
                    {"type": "component", "block": "REGULATOR-2", "gap": 0},
                    {"type": "pipe", "direction": "down", "length_mm": 200},
                    {"type": "component", "block": "meteran", "gap": 0, "scale": [10, 10]},
                ],
                "combined_dims": [{"from_seg": 1, "to_seg": 5}],
                "output_format": "dxf"
            }
        }


class IsometricGenerateResponse(BaseModel):
    success: bool
    message: str
    file_path: Optional[str] = None
    file_name: Optional[str] = None


class BlockInfo(BaseModel):
    name: str
    thumbnail_url: Optional[str] = None
    has_entry_point: bool = False
    extent: Optional[Dict[str, float]] = None


class BlockListResponse(BaseModel):
    blocks: List[BlockInfo]


class VariantDirectionsResponse(BaseModel):
    variants: Dict[str, Dict[str, float]]
    variant_scales: Dict[str, List[int]]
