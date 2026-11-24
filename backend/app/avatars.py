"""Real Blobatar gen-2 SVGs: a cached native PNG and a cached layered face. No invented data."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import struct
from typing import Any
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Response
import httpx
import resvg_py

_SIZE = 192
_MAX_SVG_BYTES = 128 * 1024
_MAX_FACE_BYTES = 256 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SEED = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_FILL = re.compile(r"#[0-9A-Fa-f]{3,8}\Z", re.ASCII)
_SVG_NS = "http://www.w3.org/2000/svg"
_ALLOWED_TAGS = {"svg", "g", "path", "circle", "ellipse", "rect", "polygon", "polyline", "line", "defs", "clipPath", "mask"}
EXPRESSIONS: tuple[str, ...] = ("idle", "happy", "thinking", "sad", "surprised")
_EXPRESSION_CONCURRENCY = 3
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


class AvatarUnavailable(RuntimeError):
    pass


class AvatarService:
    def __init__(self, cache_dir: Path, *, base_url: str = "https://blobatar.dev/avatar") -> None:
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def start(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False
        )

    async def close(self) -> None:
        pending = list(self._inflight.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def cache_path(self, seed: str) -> Path:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}-gen2-none-{_SIZE}.png"

    def face_cache_path(self, seed: str) -> Path:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}-gen2-none-face.json"

    async def png(self, seed: str) -> bytes:
        return await self._shared(f"{seed}\0png", lambda: self._png_work(seed))

    async def face(self, seed: str) -> dict:
        return await self._shared(f"{seed}\0face", lambda: self._face_work(seed))

    async def _shared(self, key: str, work: Any) -> Any:
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(work())
            self._inflight[key] = task

            def release(finished: asyncio.Task[Any]) -> None:
                if self._inflight.get(key) is finished:
                    self._inflight.pop(key, None)
                if not finished.cancelled():
                    # A disconnected requester may no longer be awaiting a failed task.
                    finished.exception()

            task.add_done_callback(release)
        # Navigating away from a row must not cancel a shared fetch for other rows.
        return await asyncio.shield(task)

    # ------------------------------------------------------------------ PNG

    @staticmethod
    def _valid_png(data: bytes) -> bool:
        return len(data) >= 24 and data.startswith(_PNG_SIGNATURE) and struct.unpack(">II", data[16:24]) == (_SIZE, _SIZE)

    @classmethod
    def _read_cache(cls, path: Path) -> bytes | None:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                path.unlink()
                return None
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if cls._valid_png(data):
            return data
        path.unlink(missing_ok=True)
        return None

    async def _png_work(self, seed: str) -> bytes:
        path = self.cache_path(seed)
        try:
            cached = await asyncio.to_thread(self._read_cache, path)
        except OSError as exc:
            raise AvatarUnavailable("头像缓存暂时无法读取，请稍后重试。") from exc
        if cached is not None:
            return cached
        try:
            text = await self._fetch_svg(seed, size=_SIZE)
            png = await asyncio.to_thread(
                resvg_py.svg_to_bytes, svg_string=text, width=_SIZE, height=_SIZE,
                skip_system_fonts=True,
            )
            if not self._valid_png(png):
                raise AvatarUnavailable("头像图像未能正确生成，请重试。")
            await asyncio.to_thread(self._write_cache, path, png)
            return png
        except AvatarUnavailable:
            raise
        except OSError as exc:
            raise AvatarUnavailable("头像缓存暂时无法写入，请稍后重试。") from exc

    # ------------------------------------------------------------------ face

    async def _face_work(self, seed: str) -> dict:
        path = self.face_cache_path(seed)
        try:
            cached = await asyncio.to_thread(self._read_face_cache, path)
        except OSError as exc:
            raise AvatarUnavailable("头像缓存暂时无法读取，请稍后重试。") from exc
        if cached is not None:
            return cached
        gate = asyncio.Semaphore(_EXPRESSION_CONCURRENCY)

        async def one(expression: str) -> tuple[str, dict]:
            async with gate:
                text = await self._fetch_svg(
                    seed, expression=None if expression == "idle" else expression
                )
                return expression, self._layers(text)

        try:
            results = await asyncio.gather(*(one(name) for name in EXPRESSIONS), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        fetched: dict[str, dict] = {}
        for name, result in zip(EXPRESSIONS, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, AvatarUnavailable):
                raise result
            if isinstance(result, BaseException):
                raise AvatarUnavailable("头像来源返回了无效图像，请稍后重试。") from result
            fetched[name] = result[1]
        body = fetched["idle"]
        face = {
            "seed": seed,
            "body": {"fill": body["body_fill"], "d": body["body_d"]},
            "eye_fill": body["eye_fill"],
            "expressions": {name: fetched[name]["eyes"] for name in EXPRESSIONS},
        }
        try:
            await asyncio.to_thread(self._write_cache, path, json.dumps(face, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            raise AvatarUnavailable("头像缓存暂时无法写入，请稍后重试。") from exc
        return face

    @classmethod
    def _layers(cls, svg: str) -> dict:
        """Split one validated Blobatar SVG into body fill/path and the two eye paths."""
        root = ET.fromstring(svg)
        groups = cls._fill_groups(root)
        if len(groups) != 2:
            raise AvatarUnavailable("头像来源的分层结构与预期不符，请稍后重试。")
        body, eyes = groups
        paths = cls._paths(body)
        if not paths:
            raise AvatarUnavailable("头像来源缺少身体轮廓，请稍后重试。")
        eye_paths = cls._paths(eyes)
        if len(eye_paths) != 2:
            raise AvatarUnavailable("头像来源的眨眼图层不完整，请稍后重试。")
        return {
            "body_fill": cls._fill(body),
            "body_d": paths[0],
            "eye_fill": cls._fill(eyes),
            "eyes": eye_paths,
        }

    @classmethod
    def _fill_groups(cls, root: ET.Element) -> list[ET.Element]:
        """Top-level painted groups; an expression wrapper only shifts and is dropped."""
        groups: list[ET.Element] = []
        for child in list(root):
            tag = child.tag.removeprefix(f"{{{_SVG_NS}}}")
            if tag != "g":
                continue
            if child.get("fill"):
                groups.append(child)
                continue
            groups.extend(cls._fill_groups(child))
        return groups

    @staticmethod
    def _paths(group: ET.Element) -> list[str]:
        found: list[str] = []
        for child in list(group):
            if child.tag != f"{{{_SVG_NS}}}path":
                continue
            data = (child.get("d") or "").strip()
            if data:
                found.append(data)
        return found

    @staticmethod
    def _fill(group: ET.Element) -> str:
        fill = (group.get("fill") or "").strip()
        if not _FILL.fullmatch(fill):
            raise AvatarUnavailable("头像来源的颜色格式无法识别，请稍后重试。")
        return fill

    @classmethod
    def _read_face_cache(cls, path: Path) -> dict | None:
        try:
            if path.stat().st_size > _MAX_FACE_BYTES:
                path.unlink()
                return None
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            face = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        if cls._valid_face(face):
            return face
        path.unlink(missing_ok=True)
        return None

    @staticmethod
    def _valid_face(face: Any) -> bool:
        if not isinstance(face, dict):
            return False
        body = face.get("body")
        expressions = face.get("expressions")
        if not isinstance(body, dict) or not isinstance(expressions, dict):
            return False
        if not (isinstance(body.get("fill"), str) and isinstance(body.get("d"), str)):
            return False
        if not isinstance(face.get("eye_fill"), str):
            return False
        for name in EXPRESSIONS:
            eyes = expressions.get(name)
            if not (isinstance(eyes, list) and len(eyes) == 2):
                return False
            if not all(isinstance(path, str) and path for path in eyes):
                return False
        return True

    # ------------------------------------------------------------------ provider

    async def _fetch_svg(self, seed: str, *, expression: str | None = None, size: int | None = None) -> str:
        if self._client is None:
            raise AvatarUnavailable("头像服务尚未就绪，请稍后重试。")
        params = {"gen": "2", "background": "none"}
        if expression:
            params["expression"] = expression
        if size:
            params["size"] = str(size)
        try:
            async with asyncio.timeout(25):
                async with self._client.stream(
                    "GET", f"{self.base_url}/{quote(seed, safe='')}",
                    params=params,
                    headers={"Accept": "image/svg+xml"},
                ) as response:
                    if response.status_code != 200:
                        raise AvatarUnavailable("Blobatar 头像服务暂不可用，请点击头像重试。")
                    if "image/svg+xml" not in response.headers.get("content-type", "").lower():
                        raise AvatarUnavailable("头像来源未返回有效的 SVG 图像。")
                    svg = bytearray()
                    async for chunk in response.aiter_bytes():
                        svg.extend(chunk)
                        if len(svg) > _MAX_SVG_BYTES:
                            raise AvatarUnavailable("头像来源返回的图像超过安全大小限制。")
            return self._validated_svg(bytes(svg))
        except AvatarUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AvatarUnavailable("暂时无法连接 Blobatar 头像服务，请点击重试。") from exc
        except (ValueError, UnicodeError, ET.ParseError) as exc:
            raise AvatarUnavailable("头像来源返回了无效图像，请稍后重试。") from exc

    @staticmethod
    def _validated_svg(svg: bytes) -> str:
        text = svg.decode("utf-8")
        upper = text.upper()
        if "\x00" in text or "<!DOCTYPE" in upper or "<!ENTITY" in upper:
            raise AvatarUnavailable("头像 SVG 包含不允许的外部资源声明。")
        root = ET.fromstring(text)
        if root.tag != f"{{{_SVG_NS}}}svg":
            raise AvatarUnavailable("头像来源不是有效 SVG。")
        count = 0
        for element in root.iter():
            count += 1
            tag = element.tag.removeprefix(f"{{{_SVG_NS}}}")
            if tag not in _ALLOWED_TAGS or count > 1024:
                raise AvatarUnavailable("头像 SVG 包含不支持的图像资源。")
            for name, value in element.attrib.items():
                local_name = name.rsplit("}", 1)[-1].lower()
                if local_name.startswith("on") or local_name == "style":
                    raise AvatarUnavailable("头像 SVG 包含不允许的交互内容。")
                if local_name == "href" and not value.startswith("#"):
                    raise AvatarUnavailable("头像 SVG 不允许引用外部资源。")
                if "url(" in value.lower() and not re.fullmatch(r"url\(#[A-Za-z0-9_-]+\)", value):
                    raise AvatarUnavailable("头像 SVG 不允许引用外部资源。")
        return text

    @staticmethod
    def _write_cache(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def create_avatar_router(cache_dir: Path, *, base_url: str = "https://blobatar.dev/avatar") -> APIRouter:
    service = AvatarService(cache_dir, base_url=base_url)

    @asynccontextmanager
    async def lifespan(_: object):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    router = APIRouter(lifespan=lifespan)

    def _checked_seed(seed: str) -> str:
        if not _SEED.fullmatch(seed) or seed in (".", ".."):
            raise HTTPException(status_code=400, detail="头像标识无效。")
        return seed

    @router.get("/avatars/{seed}.png")
    async def avatar(seed: str) -> Response:
        _checked_seed(seed)
        try:
            png = await service.png(seed)
        except AvatarUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
        return Response(content=png, media_type="image/png", headers=IMMUTABLE)

    @router.get("/avatars/{seed}/face.json")
    async def avatar_face(seed: str) -> Response:
        _checked_seed(seed)
        try:
            face = await service.face(seed)
        except AvatarUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
        return Response(
            content=json.dumps(face, ensure_ascii=False).encode("utf-8"),
            media_type="application/json",
            headers=IMMUTABLE,
        )

    return router
