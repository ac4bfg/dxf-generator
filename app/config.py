import os
from pathlib import Path
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    template_path: str = "templates/ASBUILT_SR.dxf"
    output_path: str = "output"
    host: str = "0.0.0.0"
    port: int = 8099
    debug: bool = False
    api_key: str = ""

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
