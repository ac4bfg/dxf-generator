import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent))

from app.config import get_settings
from app.routes.dxf import router as dxf_router
from app.routes.isometric import router as isometric_router


app = FastAPI(
    title="DXF Generator API",
    description="Microservice for generating DXF asbuilt documents for Service Regulator (SR)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
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
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        factory=False
    )


if __name__ == "__main__":
    main()
