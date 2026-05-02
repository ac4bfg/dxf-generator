import os
import platform
import shutil
from pathlib import Path
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    template_path: str = "templates/ASBUILT_SR.dxf"
    isometric_template_path: str = "templates/SR_POLOS.dxf"
    sk_isometric_template_path: str = "templates/SK_POLOS.dxf"
    thumbnails_path: str = "thumbnails"
    output_path: str = "output"
    # Production PDF rendering — points to the directory that holds the SHX/TTF
    # AutoCAD fonts (registered as ezdxf support_dir). Falls back to the
    # repo's `testing/autocad_fonts/` if the configured path doesn't exist.
    pdf_fonts_dir: str = "fonts"
    # Directory containing PNG overlays for OLE2FRAME placeholders, named
    # `drawing1.png` (topmost on sheet), `drawing2.png`, …
    pdf_logo_dir: str = "logo"
    host: str = "0.0.0.0"
    port: int = 8099
    debug: bool = False
    api_key: str = ""
    oda_path: str = "/usr/bin/ODAFileConverter_27.1.0.0/ODAFileConverter"
    oda_enabled: bool = True
    default_output_format: str = "dwg"
    dwg_version: str = "ACAD2018"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def get_template_path() -> Path:
    settings = get_settings()
    return Path(settings.template_path)


def get_output_path() -> Path:
    settings = get_settings()
    output = Path(settings.output_path)
    output.mkdir(parents=True, exist_ok=True)
    return output


def get_oda_path() -> str:
    settings = get_settings()
    if os.path.exists(settings.oda_path):
        return settings.oda_path
    return None


def is_oda_available() -> bool:
    settings = get_settings()
    if platform.system() == "Windows":
        return False
    return os.path.exists(settings.oda_path)
