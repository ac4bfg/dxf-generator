from pydantic import BaseModel, Field
from typing import Dict, Optional, Literal


class MaterialData(BaseModel):
    coupler: str = Field(default="0", description="Material ID 19 - Coupler 20mm")
    elbow: str = Field(default="0", description="Material ID 10 - Elbow 90° x 20mm")
    pipa: str = Field(default="0", description="Material ID 8 - Pipa PE 20mm (m)")
    sealtape: str = Field(default="0", description="Material ID 7 - Seal Tape")
    casing: str = Field(default="0", description="Material ID 21 - Casing 1 inch (m)")


class AsbuiltGenerateRequest(BaseModel):
    reff_id: str = Field(..., description="Customer reference ID")
    nama: str = Field(..., description="Customer name")
    alamat: str = Field(..., description="Customer address")
    rt: str = Field(default="", description="RT number")
    rw: str = Field(default="", description="RW number")
    kelurahan: str = Field(..., description="Kelurahan/urban village")
    sektor: str = Field(..., description="Sector code")
    no_mgrt: str = Field(..., description="MGRT serial number")
    sn_awal: str = Field(default="", description="Initial serial number")
    koordinat_tapping: str = Field(default="", description="Tapping coordinates (lat, lng)")
    materials: MaterialData = Field(default_factory=MaterialData)
    tanggal: Optional[str] = Field(default=None, description="Date string, auto-generated if not provided")
    output_format: Literal["dxf", "dwg"] = Field(default="dwg", description="Output format: 'dxf' or 'dwg' (default: dwg)")

    class Config:
        json_schema_extra = {
            "example": {
                "reff_id": "044.10.01.22.0040",
                "nama": "ISTANTO BUDI SANTOSO",
                "alamat": "Perum Griya Harapan Desa Penyangkringan RT 03 Rw 15",
                "rt": "01",
                "rw": "07",
                "kelurahan": "044 - Nawangsari",
                "sektor": "07",
                "no_mgrt": "25110607153",
                "sn_awal": "0.025",
                "koordinat_tapping": "-6.9672, 110.0741",
                "materials": {
                    "coupler": "1",
                    "elbow": "1",
                    "pipa": "20.00",
                    "sealtape": "6"
                },
                "output_format": "dwg"
            }
        }


class BulkGenerateRequest(BaseModel):
    items: list[AsbuiltGenerateRequest] = Field(..., description="List of asbuilt data to generate")
    mode: str = Field(default="zip", description="Output mode: 'zip' for individual files")
    output_format: Literal["dxf", "dwg"] = Field(default="dwg", description="Output format: 'dxf' or 'dwg' (default: dwg)")


class GenerateResponse(BaseModel):
    success: bool
    message: str
    file_path: Optional[str] = None
    file_name: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str = "1.1.0"
    template_found: bool
    oda_available: bool = False
    default_format: str = "dwg"
