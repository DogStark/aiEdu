import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent.log_config import (
    configure_logging,
    get_logger,
    new_request_id,
    reset_request_id,
    set_request_id,
)
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

env = os.getenv("ENV", "production").lower()

if env == "development":
    allowed_origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ALLOW_ORIGINS", "*"
        ).split(",")
        if origin.strip()
    ]
    allowed_origins_list = allowed_origins if allowed_origins else ["*"]
    allow_methods_list = ["*"]
else:
    allowed_origins_list = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ALLOW_ORIGINS",
            "http://localhost:3000,http://localhost:5173",
        ).split(",")
        if origin.strip()
    ] or []
    allow_methods_list = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins_list,
    allow_methods=allow_methods_list,
    allow_headers=["*"],
)


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """Assign a per-request correlation ID and emit bounded access metadata.

    The ID lets operators correlate a single request across log lines without
    any persistent identifier; it is also returned via X-Request-ID.
    """
    request_id = new_request_id()
    token = set_request_id(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            getattr(request.scope.get("route"), "path_format", None) or "<unmatched>",
            extra={
                "source_module": __name__,
                "source_function": "request_observability_middleware",
                "http_method": request.method,
                "route_template": getattr(
                    request.scope.get("route"), "path_format", None
                )
                or "<unmatched>",
                "status_code": 500,
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "outcome": "server_error",
            },
        )
        reset_request_id(token)
        raise
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    route = request.scope.get("route")
    route_template = getattr(route, "path_format", None) or "<unmatched>"
    logger.info(
        "%s %s -> %s",
        request.method,
        route_template,
        response.status_code,
        extra={
            "source_module": __name__,
            "source_function": "request_observability_middleware",
            "http_method": request.method,
            "route_template": route_template,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "outcome": (
                "server_error"
                if response.status_code >= 500
                else "client_error"
                if response.status_code >= 400
                else "ok"
            ),
        },
    )
    reset_request_id(token)
    return response


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
    # Log the exception class only: profile-error messages can embed raw
    # student IDs, and log sinks live outside managed deletion.
    logger.error(
        "Profile storage error",
        extra={
            "source_module": __name__,
            "source_function": "profile_error_handler",
            "error_type": type(exc).__name__,
            "outcome": "storage_error",
        },
    )
    return JSONResponse(status_code=500, content={"detail": "Profile storage error."})


@app.get("/")
def root():
    return {
        "service": "WordBloc AI Learning Agent",
        "status": "running",
        "docs": "/docs",
    }
