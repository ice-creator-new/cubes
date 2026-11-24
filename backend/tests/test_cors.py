"""Browser clients (web dev server, desktop window) may call the API; other origins may not."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import _origins_env
from app.main import create_app


def _client(make_backend, **overrides) -> TestClient:
    backend = make_backend()
    settings = replace(backend.settings, **overrides) if overrides else backend.settings
    return TestClient(create_app(settings))


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/workspace",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_listed_origins_pass_preflight_and_reads(make_backend) -> None:
    with _client(make_backend) as client:
        for origin in ("http://127.0.0.1:1420", "app://cubes"):
            preflight = _preflight(client, origin)
            assert preflight.status_code == 200, origin
            assert preflight.headers["access-control-allow-origin"] == origin
            response = client.get("/health", headers={"Origin": origin})
            assert response.headers["access-control-allow-origin"] == origin
            assert "access-control-allow-credentials" not in response.headers


def test_unlisted_origin_gets_no_cors_grant(make_backend) -> None:
    with _client(make_backend) as client:
        preflight = _preflight(client, "https://evil.example")
        assert "access-control-allow-origin" not in preflight.headers
        response = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers


def test_configured_origins_replace_defaults_and_refuse_wildcard(monkeypatch, make_backend) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " https://cubes.example/ , * ")
    origins = _origins_env("CORS_ORIGINS", default=("http://127.0.0.1:1420",))
    assert origins == ("https://cubes.example",)
    monkeypatch.setenv("CORS_ORIGINS", "*")
    assert _origins_env("CORS_ORIGINS", default=("http://127.0.0.1:1420",)) == ("http://127.0.0.1:1420",)
    with _client(make_backend, cors_origins=origins) as client:
        assert _preflight(client, "https://cubes.example").headers["access-control-allow-origin"] == "https://cubes.example"
        assert "access-control-allow-origin" not in _preflight(client, "http://127.0.0.1:1420").headers
