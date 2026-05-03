from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional, Tuple, Union


StartVariant = Literal["start-BR", "start-BL", "start-TR", "start-TL"]
Direction = Literal["forward", "back", "right", "left", "up", "down"]
DimSide = Literal["default", "opposite", "right", "left", "top", "bottom"]


class PipeOverlay(BaseModel):
    """Overlay block stamped on top of a pipe at a relative position.

    Used for arrows / direction markers / casings that need to appear AT a
    point along a pipe (not before/after as a series component). The pipe
    cursor is NOT advanced — the next segment continues from the pipe's
    real endpoint.
    """
    block: str = Field(..., description="Block name to insert.")
    block_by_variant: Optional[Dict["StartVariant", str]] = Field(
        None,
        description="Per start-variant block override (e.g. direction-BR / -BL)."
    )
    position: float = Field(
        0.5, ge=0.0, le=1.0,
        description=("Relative position along the pipe's VISUAL length. "
                     "0.0 = pipe start, 1.0 = pipe end, 0.5 = middle (default)."),
    )
    scale: Optional[List[float]] = Field(
        None, min_length=2, max_length=2,
        description="Optional [x, y] scale; defaults to (1, 1).",
    )
    rotation_offset: float = Field(
        0.0,
        description=("Extra rotation in degrees added to the pipe's angle. "
                     "Default 0 — overlay aligns with pipe direction."),
    )
    rotation_offset_by_variant: Optional[Dict["StartVariant", float]] = Field(
        None,
        description=("Per start-variant rotation_offset override. Useful "
                     "when different `block_by_variant` blocks were drawn "
                     "with different native orientations and need separate "
                     "tuning. Falls back to `rotation_offset` for variants "
                     "not in the map."),
    )

    class Config:
        extra = "ignore"


class BreaklineConfig(BaseModel):
    """Render this pipe segment shorter than its real length.

    Two visual styles:
      * ``zigzag``   — NTS-style break with a zigzag symbol at the midpoint.
      * ``straight`` — same short visual length but rendered as one straight
        line (uniform-template look — typical SR-style where every pipe
        appears the same on the sheet but the dimension reflects reality).

    Omit/set to None on the segment to draw the pipe at full length.
    """
    style: Literal["zigzag", "straight"] = Field(
        "zigzag",
        description="Visual style: 'zigzag' (NTS symbol) or 'straight' (no symbol).",
    )
    real_length_mm: Optional[float] = Field(
        None, gt=0,
        description=("Real pipe length shown in the DIMENSION. "
                     "If null, falls back to the segment's length_mm."),
    )
    visual_length_mm: float = Field(
        ..., gt=0,
        description="On-paper rendered length of the broken pipe (mm).",
    )

    class Config:
        extra = "ignore"


class PipeSegment(BaseModel):
    type: Literal["pipe"] = "pipe"
    length_mm: float = Field(..., gt=0, description="Pipe length in millimeters")
    direction: Optional[Direction] = Field(None, description="Direction keyword (auto-translated per variant)")
    direction_by_variant: Optional[Dict[StartVariant, Direction]] = None
    angle: Optional[float] = Field(None, ge=0, lt=360, description="Absolute angle (if no direction)")
    angle_by_variant: Optional[Dict[StartVariant, float]] = None
    no_transform: bool = False
    color: Optional[int] = Field(None, ge=1, le=256, description="ACI color number (1=red,2=yellow,3=green,4=cyan,5=blue,6=magenta,7=white). Default: cyan (4).")
    dimension: bool = False
    dimension_side: Optional[DimSide] = "default"
    bend_side: Optional[Literal["left", "right"]] = None
    bend_side_by_variant: Optional[Dict[StartVariant, Literal["left", "right"]]] = None
    breakline: Optional[BreaklineConfig] = Field(
        None,
        description=("Optional NTS breakline. Default null = pipe drawn at "
                     "full length_mm with no break symbol."),
    )
    overlays: Optional[List[PipeOverlay]] = Field(
        None,
        description=("Optional list of blocks to stamp AT positions along "
                     "this pipe (e.g. direction arrow at midpoint). The pipe "
                     "cursor is not advanced by these — they are visual "
                     "decoration only."),
    )

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


class CrossingSegment(BaseModel):
    type: Literal["crossing"] = "crossing"
    insert: Optional[List[float]] = Field(
        None, min_length=2, max_length=2,
        description="Koordinat absolut (x, y). Jika tidak diisi, pakai CROSSING_INSERT_DEFAULT per variant."
    )
    scale: Optional[List[float]] = Field(None, min_length=2, max_length=2)

    class Config:
        extra = "ignore"


Segment = Union[PipeSegment, ComponentSegment, CrossingSegment]


class CombinedDim(BaseModel):
    from_seg: int = Field(..., ge=0)
    to_seg: int = Field(..., ge=0)
    text_mm: Optional[float] = None
    side: Optional[DimSide] = "default"
    dim_offset: Optional[float] = Field(
        None, gt=0,
        description=(
            "Distance from pipe to dimension line (drawing units). "
            "Defaults to 6.0 (same as per-segment dims). "
            "Set higher (e.g. 12.0) if combined dim overlaps an individual dim."
        ),
    )

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
    sk_line_color: Optional[int] = Field(None, ge=1, le=256, description="Warna global LWPOLYLINE SK (ACI). Default: 5 (biru).")

    class Config:
        json_schema_extra = {
            "example": {
                "start_block": "start-BR",
                "start_insert": [209.52, 124.98],
                "segments": [
                    # Breakline example: real pipe is 15 m but only 1.5 m is
                    # drawn on the sheet; dimension still reads 15000.
                    {"type": "pipe", "direction": "forward", "length_mm": 15000,
                     "dimension": True,
                     "breakline": {"real_length_mm": 15000, "visual_length_mm": 1500}},
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


class BulkPdfItem(BaseModel):
    module: Literal["SR", "SK"] = "SR"
    start_block: Optional[str] = "start-BR"
    start_insert: Optional[Tuple[float, float]] = None
    start_rotation: float = 0
    segments: List[Segment] = Field(..., min_length=1)
    combined_dims: List[CombinedDim] = []
    customer_data: Optional[Dict[str, Any]] = None

    class Config:
        extra = "ignore"


class BulkPdfRequest(BaseModel):
    items: List[BulkPdfItem] = Field(..., min_length=1)
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
