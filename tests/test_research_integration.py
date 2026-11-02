"""The research router is mounted on the real app, and the n8n intake workflow
points at it without carrying secrets."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "examples" / "n8n" / "clip-factory-topic-intake.json"


def test_app_mounts_research_router():
    app_module = pytest.importorskip("app")
    # Newer FastAPI keeps included routers as nested objects in app.routes;
    # the OpenAPI schema lists every reachable path either way.
    paths = set(app_module.app.openapi()["paths"])
    assert "/api/research/topic-jobs" in paths
    assert "/api/research/sources/{source_id}/media-analysis" in paths


def test_app_research_api_fails_closed(monkeypatch):
    app_module = pytest.importorskip("app")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("RESEARCH_API_TOKEN", raising=False)
    response = TestClient(app_module.app).post("/api/research/topic-jobs", json={"topic": "x"})
    assert response.status_code == 503


def test_intake_workflow_is_wired_and_secret_free():
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    names = {node["name"] for node in workflow["nodes"]}
    for source, outputs in workflow["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            assert all(link["node"] in names for link in branch)

    nodes = {node["name"]: node for node in workflow["nodes"]}
    webhook = nodes["Topic intake"]
    assert webhook["parameters"]["authentication"] == "headerAuth"
    assert webhook["parameters"]["responseMode"] == "responseNode"
    request = nodes["Create research job"]
    assert request["parameters"]["url"].endswith("/api/research/topic-jobs")
    assert request["credentials"]["httpHeaderAuth"]["name"] == "OpenShorts research token"
    assert workflow["active"] is False

    text = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("osk_", "sk_", "Bearer ", "api.openshorts.app"):
        assert forbidden not in text
