import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent.log_config import configure_logging, get_logger
from agent.privacy import (
    get_retention_sweep_interval_hours,
    purge_expired_profiles,
)
from agent.profiler import (
    ConsentRequiredError,
    InvalidConsentError,
    InvalidStudentIdError,
    ProfileError,
    ProfileNotFoundError,
)
from api.routes import router

# Configure structured logging once at startup.
configure_logging()

logger = get_logger(__name__)


async def _retention_worker(interval_hours: float):
    while True:
        await asyncio.sleep(interval_hours * 60 * 60)
        result = await asyncio.to_thread(purge_expired_profiles)
        logger.info(
            "Retention sweep scanned %s profiles and purged %s.",
            result["scanned_profiles"],
            len(result["purged_student_ids"]),
        )
        if result["errors"]:
            logger.error("Retention sweep encountered %s error(s).", len(result["errors"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sweep once at startup and then periodically for long-running deployments.
    result = await asyncio.to_thread(purge_expired_profiles)
    logger.info(
        "Startup retention sweep scanned %s profiles and purged %s.",
        result["scanned_profiles"],
        len(result["purged_student_ids"]),
    )
    interval_hours = get_retention_sweep_interval_hours()
    retention_task = asyncio.create_task(_retention_worker(interval_hours))
    try:
        yield
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task


app = FastAPI(
    title="WordBloc AI Learning Agent",
    description="AI agent that studies kids' learning ability and recommends words for the WordBloc game.",
    version="1.1.0",
    lifespan=lifespan,
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS", "http://localhost:3000,http://localhost:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(ProfileNotFoundError)
async def profile_not_found_handler(request: Request, exc: ProfileNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ConsentRequiredError)
async def consent_required_handler(request: Request, exc: ConsentRequiredError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(InvalidConsentError)
async def invalid_consent_handler(request: Request, exc: InvalidConsentError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(InvalidStudentIdError)
async def invalid_student_id_handler(request: Request, exc: InvalidStudentIdError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ProfileError)
async def profile_error_handler(request: Request, exc: ProfileError):
    logger.error("Profile storage error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Profile storage error."})


@app.get("/")
def root():
    return {
        "service": "WordBloc AI Learning Agent",
        "status": "running",
        "docs": "/docs",
    }
