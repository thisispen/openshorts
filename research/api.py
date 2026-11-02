"""`/api/research` routes: topic jobs, source metadata, rights-gated media analysis,
classification, cohort statistics and the channel registry.

Auth: every route needs ``X-Research-Token`` equal to ``RESEARCH_API_TOKEN``
(constant-time). With no token configured the API answers 503, so a self-host
instance never exposes it open. Handlers are sync so FastAPI runs the
database work in its threadpool.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from . import db
from .models import ChannelIn, MediaAnalysisIn, SourceBatchIn, TopicJobCreate
from .rights import RightsError
from .service import BudgetExceeded, Conflict, NotFound, RateLimited, ResearchService
from .settings import load_settings

router = APIRouter(prefix="/api/research", tags=["research"])
_initialised: set[str] = set()


def get_service() -> ResearchService:
    settings = load_settings()
    engine = db.get_engine()
    if settings.database_url not in _initialised:
        db.init_schema(engine)
        _initialised.add(settings.database_url)
    return ResearchService(engine, settings)


def require_token(x_research_token: Optional[str] = Header(None, alias="X-Research-Token")) -> None:
    expected = load_settings().api_token
    if not expected:
        raise HTTPException(status_code=503, detail="Research API disabled: set RESEARCH_API_TOKEN on the server.")
    if not x_research_token or not hmac.compare_digest(x_research_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Research-Token.")


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, BudgetExceeded):
        return HTTPException(status_code=402, detail=str(exc))
    if isinstance(exc, RightsError):
        return HTTPException(status_code=403, detail=str(exc))
    raise exc


_DOMAIN_ERRORS = (NotFound, Conflict, RateLimited, BudgetExceeded, RightsError)
_auth = [Depends(require_token)]


@router.post("/topic-jobs", status_code=201, dependencies=_auth)
def create_topic_job(
    body: TopicJobCreate,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", min_length=8, max_length=128),
    service: ResearchService = Depends(get_service),
):
    try:
        job, created = service.create_topic_job(body, idempotency_key)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)
    if not created:
        response.status_code = 200
    return {"job_id": job["id"], "status": job["status"], "created": created, "job": job}


@router.get("/topic-jobs/{job_id}", dependencies=_auth)
def get_topic_job(job_id: str, service: ResearchService = Depends(get_service)):
    try:
        return service.get_topic_job(job_id)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.post("/topic-jobs/{job_id}/sources", dependencies=_auth)
def add_sources(job_id: str, body: SourceBatchIn, service: ResearchService = Depends(get_service)):
    try:
        return service.add_sources(job_id, body)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.get("/topic-jobs/{job_id}/sources", dependencies=_auth)
def list_sources(job_id: str, service: ResearchService = Depends(get_service)):
    try:
        service.get_topic_job(job_id)
        return {"sources": service.list_sources(job_id)}
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.post("/sources/{source_id}/media-analysis", dependencies=_auth)
def media_analysis(source_id: str, body: MediaAnalysisIn, service: ResearchService = Depends(get_service)):
    try:
        return service.record_media_analysis(source_id, body)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.post("/topic-jobs/{job_id}/classify", dependencies=_auth)
def classify(job_id: str, service: ResearchService = Depends(get_service)):
    try:
        return service.classify_sources(job_id)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.get("/topic-jobs/{job_id}/cohort", dependencies=_auth)
def cohort(job_id: str, include_synthetic: bool = Query(False), service: ResearchService = Depends(get_service)):
    try:
        return service.cohort(job_id, include_synthetic)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.get("/spend", dependencies=_auth)
def spend(service: ResearchService = Depends(get_service)):
    return service.spend_summary()


@router.post("/channels", status_code=201, dependencies=_auth)
def register_channel(body: ChannelIn, service: ResearchService = Depends(get_service)):
    try:
        return service.register_channel(body)
    except _DOMAIN_ERRORS as exc:
        raise _http(exc)


@router.get("/channels", dependencies=_auth)
def list_channels(service: ResearchService = Depends(get_service)):
    return {"channels": service.list_channels()}
