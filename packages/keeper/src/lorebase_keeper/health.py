"""Liveness and readiness endpoints, served at the root (outside the API prefix)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.readiness import ReadinessCheck, run_checks

router = APIRouter(tags=["health"])


class Liveness(BaseModel):
    """The process is up."""

    status: Literal["ok"] = "ok"


class CheckReport(BaseModel):
    """Outcome of one dependency check."""

    status: Literal["ok", "not_configured", "failed"]
    detail: str | None = None


class Readiness(BaseModel):
    """Whether the service can take traffic, with the outcome of each check."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckReport]


@router.get("/healthz", summary="Liveness", response_model=Liveness)
async def healthz() -> Liveness:
    """The process answers requests. Never touches dependencies."""
    return Liveness()


@router.get(
    "/readyz",
    summary="Readiness",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "A dependency check failed."}},
)
async def readyz(request: Request, keeper: KeeperSettingsDep) -> JSONResponse:
    """Dependencies are usable: the database is reachable and migrations are current."""
    checks: list[ReadinessCheck] = request.app.state.readiness_checks
    results = await run_checks(checks, keeper.readiness.timeout_s)
    ready = all(result.passed for result in results.values())
    report = Readiness(
        status="ready" if ready else "not_ready",
        checks={
            name: CheckReport(status=result.status.value, detail=result.detail)
            for name, result in results.items()
        },
    )
    return JSONResponse(report.model_dump(), status_code=200 if ready else 503)
