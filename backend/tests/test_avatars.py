"""Consumer-visible avatar cache and untrusted-SVG boundaries, isolated from live data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import struct
import threading
import time
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><rect width="100" height="100" rx="30" fill="#eaf0fa"/><path d="M20 50C20 10 80 10 80 50C80 90 20 90 20 50Z" fill="#527be0"/><circle cx="40" cy="45" r="4"/><circle cx="60" cy="45" r="4"/></svg>'


@contextmanager
def upstream(body=SVG, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(0.04)
            self.send_response(status)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/avatar", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def avatar_app(cache_dir, base_url):
    from app.avatars import create_avatar_router
    app = FastAPI()
    app.include_router(create_avatar_router(cache_dir, base_url=base_url))
    return app


def test_avatar_is_native_png_and_cached_across_service_restart(tmp_path):
    with upstream() as (base, requests):
        with TestClient(avatar_app(tmp_path / "avatars", base)) as client:
            with ThreadPoolExecutor(max_workers=6) as pool:
                replies = list(pool.map(lambda _: client.get("/avatars/agent-stable-id.png"), range(6)))
            assert all(reply.status_code == 200 for reply in replies)
            assert struct.unpack(">II", replies[0].content[16:24]) == (192, 192)
            assert replies[0].headers["content-type"] == "image/png"
            assert "immutable" in replies[0].headers["cache-control"]
            assert len(requests) == 1  # Concurrent UI rows must not exhaust provider rate limits.
            assert "gen=2" in requests[0] and "background=none" in requests[0]
    # The real source is now unreachable, but a fresh process-equivalent router can display it.
    with TestClient(avatar_app(tmp_path / "avatars", base)) as restarted:
        cached = restarted.get("/avatars/agent-stable-id.png")
        assert cached.status_code == 200
        assert cached.content == replies[0].content
        assert struct.unpack(">II", cached.content[16:24]) == (192, 192)


def test_avatar_provider_failure_is_not_a_fake_image(tmp_path):
    with upstream(status=429) as (base, _):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/member-id.png")
    assert response.status_code == 503
    assert "detail" in response.json()
    assert "image/" not in response.headers["content-type"]


def test_avatar_rejects_invalid_seed_before_fetching(tmp_path):
    with upstream() as (base, requests):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/bad!seed.png")
    assert response.status_code == 400
    assert requests == []


def test_avatar_never_renders_external_resource_references(tmp_path):
    external = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>'
    with upstream(body=external) as (base, _):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/member-id.png")
    assert response.status_code == 503
    assert "头像" in response.json()["detail"]
    assert not list(tmp_path.glob("*.png"))


# ------------------------------------------------------- layered face (face.json)

EXPRESSION_EYES = {
    "idle": ("M36 45h8", "M56 45h8"),
    "happy": ("M34 46q6 -6 12 0", "M54 46q6 -6 12 0"),
    "thinking": ("M36 43h8", "M56 46h8"),
    "sad": ("M34 48q6 6 12 0", "M54 48q6 6 12 0"),
    "surprised": ("M36 41h8", "M56 41h8"),
}


def face_svg(expression: str) -> bytes:
    eyes = EXPRESSION_EYES.get(expression, EXPRESSION_EYES["idle"])
    inner = (
        '<g fill="#527be0"><path d="M20 50C20 10 80 10 80 50C80 90 20 90 20 50Z"/></g>'
        f'<g fill="#0f172a"><path d="{eyes[0]}"/><path d="{eyes[1]}"/></g>'
    )
    if expression != "idle":
        inner = f'<g transform="translate(0 -2.2)">{inner}</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{inner}</svg>'.encode()


@contextmanager
def expression_upstream(body_for=face_svg, status=200):
    requests: list[tuple[str, str]] = []
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            expression = (parse_qs(parsed.query).get("expression") or ["idle"])[0]
            with lock:
                requests.append((parsed.path, expression))
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.08)
                body = body_for(expression)
                self.send_response(status)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    state["active"] -= 1

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/avatar", requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_face_json_is_a_layered_cache_friendly_payload(tmp_path):
    with expression_upstream() as (base, requests, state):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/agent-stable-id/face.json")
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("application/json")
            assert "immutable" in response.headers["cache-control"]
            face = response.json()

            assert face["seed"] == "agent-stable-id"
            assert face["body"]["fill"] == "#527be0"
            assert face["body"]["d"].startswith("M20 50C20 10")
            assert face["eye_fill"] == "#0f172a"
            assert sorted(face["expressions"]) == [
                "happy",
                "idle",
                "sad",
                "surprised",
                "thinking",
            ]
            # Each expression carries its own two eye paths; the wrapper translate is dropped.
            for name, eyes in face["expressions"].items():
                assert eyes == list(EXPRESSION_EYES[name]), name
            assert face["expressions"]["idle"] != face["expressions"]["happy"]

            # Every expression was really fetched once; idle carries no expression parameter.
            assert len(requests) == 5
            fetched = {expression for _, expression in requests}
            assert fetched == set(EXPRESSION_EYES)
            assert all(seed == "/avatar/agent-stable-id" for seed, _ in requests)
            # Five expressions must not stampede the provider.
            assert state["peak"] <= 3

    # A fresh process-equivalent router serves the same tree from disk, upstream gone.
    with TestClient(avatar_app(tmp_path, base)) as restarted:
        cached = restarted.get("/avatars/agent-stable-id/face.json")
    assert cached.status_code == 200
    assert cached.json() == face
    cached_files = list(tmp_path.glob("*-gen2-none-face.json"))
    assert len(cached_files) == 1


def test_face_json_provider_failure_is_not_fake_data(tmp_path):
    with expression_upstream(status=429) as (base, _, _):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/member-id/face.json")
    assert response.status_code == 503
    assert "头像" in response.json()["detail"]
    assert not list(tmp_path.glob("*-face.json"))
    assert not list(tmp_path.glob("*.json"))


def test_face_json_rejects_an_incomplete_layer_tree(tmp_path):
    def broken(expression: str) -> bytes:
        body = (
            '<g fill="#527be0"><path d="M20 50C20 10 80 10 80 50Z"/></g>'
            '<g fill="#0f172a"><path d="M36 45h8"/></g>'
        )
        return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{body}</svg>'.encode()

    with expression_upstream(body_for=broken) as (base, _, _):
        with TestClient(avatar_app(tmp_path, base)) as client:
            response = client.get("/avatars/member-id/face.json")
    assert response.status_code == 503
    assert "头像" in response.json()["detail"]
    assert not list(tmp_path.glob("*.json"))


def test_face_json_rejects_invalid_seed_before_fetching(tmp_path):
    with expression_upstream() as (base, requests, _):
        with TestClient(avatar_app(tmp_path, base)) as client:
            assert client.get("/avatars/bad!seed/face.json").status_code == 400
    assert requests == []
