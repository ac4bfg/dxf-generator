import os
import sys
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

# Suppress noisy font-table warnings emitted by fonttools when ezdxf
# parses TrueType fonts with minor 'name' table offset mismatches.
warnings.filterwarnings(
    "ignore",
    message=r".*'name' table stringOffset incorrect.*",
)
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    module=r"fontTools.*",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

from app.config import get_settings
from app.routes.dxf import router as dxf_router
from app.routes.isometric import router as isometric_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Configure fonts and apply ezdxf patches once at server startup.

    Without this, the first PDF/SVG request pays the full font-scan cost.
    Subsequent requests hit the module-level caches in pdf_renderer and
    dxf_to_svg and return immediately.
    """
    settings = get_settings()
    try:
        from app.services.pdf_renderer import _apply_ezdxf_patches, configure_ezdxf_fonts
        _apply_ezdxf_patches()
        font_dir = Path(getattr(settings, 'pdf_fonts_dir', '') or '')
        for fallback in (font_dir, Path('assets/fonts'), Path('testing/autocad_fonts')):
            if fallback.is_dir():
                configure_ezdxf_fonts(fallback)
                break
    except Exception:
        pass  # non-fatal: fonts will be configured on first request as fallback
    yield  # server runs here


app = FastAPI(
    title="DXF Generator API",
    description="Microservice for generating DXF asbuilt documents for Service Regulator (SR)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dxf_router)
app.include_router(isometric_router)


@app.get("/")
async def root():
    return JSONResponse({
        "service": "DXF Generator API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs"
    })


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"}
    )


def main():
    settings = get_settings()
    print(f"Starting DXF Generator API on {settings.host}:{settings.port}")
    print(f"Template path: {settings.template_path}")
    print(f"Output path: {settings.output_path}")
    print(f"Debug mode: {settings.debug}")

    import uvicorn
    # reload and workers>1 are mutually exclusive in uvicorn.
    # In debug mode keep 1 worker; in production use configured worker count.
    worker_count = 1 if settings.debug else settings.workers
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=worker_count,
        reload=settings.debug,
        factory=False
    )


if __name__ == "__main__":
    main()
