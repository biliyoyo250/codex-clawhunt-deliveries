"""Integration API. Inject a principal verified by the platform's authentication layer."""
from dataclasses import asdict
from typing import Annotated, Callable
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .clock import SystemClock
from .models import Handover
from .rules import Thresholds
from .service import (Actor, NotFoundError, PermissionDenied, claim_handover,
                      get_config, require_scope, transition_handover, update_config)


def trusted_principal() -> Actor:
    """Fail closed. Never derive roles or tenant scope from untrusted request headers."""
    raise HTTPException(status_code=401, detail="Verified platform principal required")


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    claim_reminder_hours: int = Field(default=24, gt=0, le=2_147_483_647)
    claim_escalation_hours: int = Field(default=48, gt=0, le=2_147_483_647)
    returned_info_hours: int = Field(default=48, gt=0, le=2_147_483_647)
    urgent_days: int = Field(default=3, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def ordered(self):
        Thresholds(**self.model_dump())
        return self


class StateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


def create_app(session_factory, clock=None, principal_provider: Callable | None = None) -> FastAPI:
    clock = clock or SystemClock()
    app = FastAPI(title="Handover reminder reference integration", version="1.0.0")
    if principal_provider is not None:
        app.dependency_overrides[trusted_principal] = principal_provider

    @app.exception_handler(PermissionDenied)
    async def forbidden(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok", "notification_adapter": "explicitly_injected"}

    @app.get("/orgs/{org_id}/sites/{site_id}/handover-reminder-config")
    def read_config(org_id: UUID, site_id: UUID, actor: Annotated[Actor, Depends(trusted_principal)]):
        require_scope(actor, org_id, site_id)
        with session_factory() as session:
            return asdict(get_config(session, org_id, site_id))

    @app.put("/orgs/{org_id}/sites/{site_id}/handover-reminder-config")
    def write_config(org_id: UUID, site_id: UUID, body: ConfigBody,
                     actor: Annotated[Actor, Depends(trusted_principal)]):
        with session_factory.begin() as session:
            return asdict(update_config(session, org_id, site_id, Thresholds(**body.model_dump()), actor, clock.now()))

    def authorize_handover(session, org_id, site_id, handover_id, actor):
        require_scope(actor, org_id, site_id)
        if not ({"site_lead", "handover_operator"} & set(actor.roles)):
            raise PermissionDenied("Handover operator role required")
        row = session.get(Handover, handover_id)
        if row is None or row.deleted_at is not None or row.org_id != org_id or row.site_id != site_id:
            raise NotFoundError("Handover was not found in this scope")

    def state_result(row):
        return {"id": str(row.id), "status": row.status, "status_epoch": row.status_epoch,
                "status_changed_at": row.status_changed_at, "notified_levels": row.notified_levels,
                "claimed_at": row.claimed_at, "handover_duration_minutes": row.handover_duration_minutes,
                "exceeded_24h": row.exceeded_24h}

    @app.post("/orgs/{org_id}/sites/{site_id}/handovers/{handover_id}/transition")
    def change_state(org_id: UUID, site_id: UUID, handover_id: UUID, body: StateBody,
                     actor: Annotated[Actor, Depends(trusted_principal)]):
        with session_factory.begin() as session:
            authorize_handover(session, org_id, site_id, handover_id, actor)
            try:
                return state_result(transition_handover(session, handover_id, body.status, clock.now(), actor.user_id))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/orgs/{org_id}/sites/{site_id}/handovers/{handover_id}/claim")
    def claim(org_id: UUID, site_id: UUID, handover_id: UUID,
              actor: Annotated[Actor, Depends(trusted_principal)]):
        with session_factory.begin() as session:
            authorize_handover(session, org_id, site_id, handover_id, actor)
            try:
                return state_result(claim_handover(session, handover_id, clock.now(), actor.user_id))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app
