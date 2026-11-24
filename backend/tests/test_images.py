"""Attachments: upload kinds and limits, sending, what models see, and cleanup."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from urllib.parse import quote

from fake_upstream import call, tool_calls
from app import attachments as attachment_rules
from app.agent import history as history_rules
from app.store.db import SCHEMA_VERSION
from helpers import (
    agent_messages,
    client_for,
    get_conversation,
    get_workspace,
    store_of,
    wait_for_run,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


async def upload(client, data: bytes, content_type: str = "image/png", name: str = "") -> dict:
    headers = {"Content-Type": content_type}
    if name:
        headers["X-Attachment-Name"] = quote(name)
    response = await client.post("/attachments", content=data, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def send(client, conversation_id: str, content: str, ids: list[str]) -> dict:
    response = await client.post(
        f"/conversations/{conversation_id}/runs",
        json={"content": content, "attachment_ids": ids},
    )
    assert response.status_code == 202, response.text
    return await wait_for_run(client, response.json()["id"])


async def enable_vision(client, model: str = "test-model") -> None:
    providers = (await client.get("/providers")).json()["providers"]
    provider = next(item for item in providers if model in item["models"])
    patched = await client.patch(
        f"/providers/{provider['id']}", json={"model_overrides": {model: {"images": True}}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["model_overrides"][model]["images"] is True


def parts(request: dict, role: str = "user") -> list:
    """Every content part of the request's messages of one role, flattened."""
    found: list = []
    for message in request["messages"]:
        if message["role"] != role:
            continue
        content = message.get("content")
        found.extend(content if isinstance(content, list) else [{"type": "text", "text": content or ""}])
    return found


def image_blocks(request: dict) -> list[str]:
    return [part["image_url"]["url"] for part in parts(request) if part.get("type") == "image_url"]


def texts(request: dict, role: str = "user") -> str:
    return "\n".join(str(part.get("text") or "") for part in parts(request, role))


def test_upload_sniffs_the_type_dedupes_files_and_refuses_bad_input(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            kinds = []
            for data in (PNG, JPEG, GIF, WEBP):
                kinds.append((await upload(client, data, "application/octet-stream"))["mime"])
            assert kinds == ["image/png", "image/jpeg", "image/gif", "image/webp"]

            # The header decides, not the declared type.
            lying = await upload(client, PNG, "image/jpeg")
            assert lying["mime"] == "image/png"
            fetched = await client.get(f"/attachments/{lying['id']}")
            assert fetched.status_code == 200
            assert fetched.content == PNG
            assert fetched.headers["content-type"] == "image/png"

            binary = await client.post(
                "/attachments", content=b"\xff\xfe\x00\x01", headers={"Content-Type": "text/plain"}
            )
            assert binary.status_code == 415
            assert binary.json()["detail"] == "只支持图片、PDF 和文本文件（代码、Markdown、CSV、JSON 等）。"
            big = await client.post(
                "/attachments", content=PNG + b"\x00" * (5 * 1024 * 1024), headers={"Content-Type": "image/png"}
            )
            assert big.status_code == 413
            assert big.json()["detail"] == "图片超过 5 MB，请压缩后再发。"

        # Two uploads of the same bytes share one file.
        assert len([path for path in store.attachments_dir.iterdir() if path.is_file()]) == 4

    asyncio.run(body())


def test_sending_images_binds_them_and_refuses_reuse(make_backend, upstream) -> None:
    upstream.script = [{"content": "看到了。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            first = await upload(client, PNG)
            second = await upload(client, JPEG)
            # Images alone are a message; the model here cannot see pixels, and that is fine.
            run = await send(client, direct_id, "", [first["id"], second["id"]])
            assert run["status"] == "completed", run

            detail = await get_conversation(client, direct_id)
            user = next(m for m in detail["messages"] if m["kind"] == "user")
            assert [image["id"] for image in user["attachments"]] == [first["id"], second["id"]]

            url = f"/conversations/{direct_id}/runs"
            again = await client.post(url, json={"content": "再发一次", "attachment_ids": [first["id"]]})
            assert again.status_code == 422
            assert again.json()["detail"] == "附件已失效，请重新添加。"
            missing = await client.post(url, json={"content": "x", "attachment_ids": ["att_000000000000"]})
            assert missing.status_code == 422
            many = [(await upload(client, PNG + bytes([n])))["id"] for n in range(5)]
            too_many = await client.post(url, json={"content": "x", "attachment_ids": many})
            assert too_many.status_code == 422
            assert "4 个附件" in too_many.json()["detail"]
            empty = await client.post(url, json={"content": "  "})
            assert empty.status_code == 422

        # A model that cannot see images gets the label only, so it can still pass the id on.
        request = upstream.requests[0]
        assert image_blocks(request) == []
        assert f"[图片 {first['id']}，这里看不到内容]" in texts(request)

    asyncio.run(body())


def test_a_vision_model_sees_the_newest_four_images_as_pixels(make_backend, upstream) -> None:
    upstream.script = [{"content": "好的。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            older = [await upload(client, PNG + bytes([n])) for n in range(2)]
            await send(client, direct_id, "前两张", [image["id"] for image in older])
            newer = [await upload(client, JPEG + bytes([n])) for n in range(4)]
            await send(client, direct_id, "后四张", [image["id"] for image in newer])

            request = upstream.requests[-1]
            urls = image_blocks(request)
            assert len(urls) == 4
            assert all(url.startswith("data:image/jpeg;base64,") for url in urls)
            assert base64.b64decode(urls[0].split(",", 1)[1]) == JPEG + bytes([0])
            labels = texts(request)
            for image in newer:
                assert f"[图片 {image['id']}]" in labels
            for image in older:
                assert f"[图片 {image['id']}，这里看不到内容]" in labels

    asyncio.run(body())


def test_anthropic_receives_image_blocks(make_backend, upstream) -> None:
    upstream.anthropic_script = [{"content": "收到图片。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/providers",
                json={
                    "name": "Anthropic",
                    "protocol": "anthropic-messages",
                    "base_url": upstream.root_url,
                    "api_key": "sk-ant-test",
                    "models": ["claude-x"],
                    "model_overrides": {"claude-x": {"images": True}},
                },
            )
            assert created.status_code == 201, created.text
            supervisor = store_of(backend).supervisor()
            await client.patch(
                f"/agents/{supervisor['id']}",
                json={"model_ref": {"provider_id": created.json()["id"], "model": "claude-x"}},
            )
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            image = await upload(client, PNG)
            await send(client, direct_id, "这张图是什么", [image["id"]])

        blocks = upstream.anthropic_requests[-1]["messages"][-1]["content"]
        assert {"type": "text", "text": f"[图片 {image['id']}]"} in blocks
        images = [block for block in blocks if block["type"] == "image"]
        assert images == [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()},
            }
        ]

    asyncio.run(body())


def test_the_supervisor_forwards_an_image_and_gets_one_back_in_a_private_chat(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="设计助手", instructions="看图", kind="member")
    elsewhere = store.create_conversation(kind="group", title="素材群", purpose="存图", member_ids=[member["id"]])
    holder: dict[str, str] = {}

    def forward(requests, index):
        return tool_calls(
            call("call_1", "direct_message", {"agent_id": member["id"], "content": "看看这张", "image_ids": [holder["user"]]})
        )

    def answer_with_image(requests, index):
        return {"content": f"改好了，附上新版。\n<images>{holder['member']} att_ffffffffffff</images>\n<mood>happy</mood>"}

    def report_back(requests, index):
        return {"content": f"设计助手发回了新版。\n<images>{holder['member']}</images>"}

    upstream.script = [forward, answer_with_image, report_back]

    async def body() -> None:
        async with client_for(backend) as client:
            await enable_vision(client)
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            # The member saw its own image somewhere else first; that is what it may pass on.
            own = await upload(client, JPEG)
            holder["member"] = own["id"]
            seeded = store.append_message(
                conversation_id=elsewhere["id"], author_id=None, author_name="你", avatar_seed=None,
                kind="user", content="素材", status="complete",
            )
            store.attach_to_message(seeded["id"], [own["id"]])
            sent = await upload(client, PNG)
            holder["user"] = sent["id"]
            run = await send(client, direct_id, "让设计助手看看", [sent["id"]])
            assert run["status"] == "completed", run

            private = next(c for c in (await get_workspace(client))["conversations"] if c["kind"] == "private")
            messages = (await get_conversation(client, private["id"]))["messages"]
            assert [image["id"] for image in messages[0]["attachments"]] == [sent["id"]]
            reply = messages[1]
            # The tag is gone from the text, the made-up id is dropped.
            assert reply["content"] == "改好了，附上新版。"
            assert [image["id"] for image in reply["attachments"]] == [own["id"]]

            final = agent_messages(await get_conversation(client, direct_id))[-1]
            assert final["content"] == "设计助手发回了新版。"
            assert [image["id"] for image in final["attachments"]] == [own["id"]]

        # The member's request carried the forwarded pixels.
        assert len(image_blocks(upstream.requests[1])) == 1
        # The supervisor's next request: the tool result names the image, then the pixels follow.
        followup = upstream.requests[2]
        tool_turn = next(m for m in followup["messages"] if m["role"] == "tool")
        assert own["id"] in tool_turn["content"]
        last = followup["messages"][-1]
        assert last["role"] == "user" and isinstance(last["content"], list)
        assert last["content"][0] == {"type": "text", "text": "[私聊里收到的图片]"}
        assert any(part.get("type") == "image_url" for part in last["content"])
        # That turn lives in the request only, never in the database.
        assert all("私聊里收到的图片" not in m["content"] for m in store.list_messages(direct_id))

    asyncio.run(body())


def test_a_bot_cannot_forward_an_image_it_never_saw(make_backend, upstream) -> None:
    backend = make_backend()
    store = store_of(backend)
    member = store.create_agent(name="甲", instructions="x", kind="member")
    holder: dict[str, str] = {}

    upstream.script = [
        lambda requests, index: tool_calls(
            call("call_1", "direct_message", {"agent_id": member["id"], "content": "看", "image_ids": [holder["id"]]})
        ),
        {"content": "那张图我看不到。"},
    ]

    async def body() -> None:
        async with client_for(backend) as client:
            holder["id"] = (await upload(client, PNG))["id"]
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            response = await client.post(f"/conversations/{direct_id}/runs", json={"content": "转给甲"})
            await wait_for_run(client, response.json()["id"])
            assert not [c for c in (await get_workspace(client))["conversations"] if c["kind"] == "private"]

        result = next(m for m in upstream.requests[1]["messages"] if m["role"] == "tool")
        assert f"图片 {holder['id']} 不在你能看到的对话里" in result["content"]

    asyncio.run(body())


def test_startup_drops_stale_unbound_uploads_and_their_files(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            kept = await upload(client, PNG)
            store.attach_to_message(
                store.append_message(
                    conversation_id=direct_id, author_id=None, author_name="你", avatar_seed=None,
                    kind="user", content="留着", status="complete",
                )["id"],
                [kept["id"]],
            )
            stale = await upload(client, JPEG)
            fresh = await upload(client, GIF)
            return kept, stale, fresh

    kept, stale, fresh = asyncio.run(body())
    day_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 24 * 3600))
    with store._transaction() as tx:  # noqa: SLF001 - ageing rows is the point of this test
        tx.execute("UPDATE attachments SET created_at = ? WHERE id IN (?, ?)", (day_ago, kept["id"], stale["id"]))
    (store.attachments_dir / "orphan").write_bytes(b"x")
    backend.restart()
    store = store_of(backend)
    assert store.get_attachment(kept["id"]) is not None
    assert store.get_attachment(fresh["id"]) is not None
    assert store.get_attachment(stale["id"]) is None
    names = set(os.listdir(store.attachments_dir))
    assert "orphan" not in names
    assert {store.get_attachment(kept["id"])["sha256"], store.get_attachment(fresh["id"])["sha256"]} == names


# ------------------------------------------------------------------ files


def test_upload_accepts_pdfs_and_text_with_their_names(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            pdf = await upload(client, PDF, "application/pdf", "季度 报告.pdf")
            assert pdf["mime"] == "application/pdf"
            assert pdf["size"] == len(PDF)
            assert pdf["name"] == "季度 报告.pdf"
            fetched = await client.get(f"/attachments/{pdf['id']}")
            assert fetched.content == PDF
            assert fetched.headers["content-type"] == "application/pdf"
            # The name travels in the RFC 5987 form, so any character survives.
            assert fetched.headers["content-disposition"] == (
                "inline; filename*=UTF-8''" + quote("季度 报告.pdf", safe="")
            )

            script = "# 注释\nprint('hi')\n".encode()
            text = await upload(client, script, "application/octet-stream", "script.py")
            assert text["mime"] == "text/plain"
            assert text["name"] == "script.py"

            # A filename is a name, not a path, and control characters do not survive.
            odd = await upload(client, b"x = 1\n", "text/plain", "../../a\x01b.py")
            assert odd["name"] == "ab.py"
            long = await upload(client, b"x = 1\n", "text/plain", "n" * 200 + ".py")
            assert len(long["name"]) == attachment_rules.MAX_NAME_CHARS

            # Images keep their name empty when the client sends none.
            plain = await upload(client, PNG)
            assert plain["name"] == ""
            assert "content-disposition" not in (await client.get(f"/attachments/{plain['id']}")).headers

            # A BOM is allowed: it is still a text file.
            bom = await upload(client, b"\xef\xbb\xbfhello\n", "text/plain")
            assert bom["mime"] == "text/plain"

    asyncio.run(body())


def test_upload_refuses_what_it_cannot_take_and_names_the_limit(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            big_image = await client.post(
                "/attachments", content=PNG + b"\x00" * (5 * 1024 * 1024), headers={"Content-Type": "image/png"}
            )
            assert big_image.status_code == 413
            assert big_image.json()["detail"] == "图片超过 5 MB，请压缩后再发。"

            big_text = await client.post(
                "/attachments",
                content=b"a" * (20 * 1024 * 1024 + 1),
                headers={"Content-Type": "text/plain"},
            )
            assert big_text.status_code == 413
            assert big_text.json()["detail"] == "文件超过 20 MB，请分开发送。"

            binary = await client.post("/attachments", content=b"\x00\x01\x02", headers={"Content-Type": "application/pdf"})
            assert binary.status_code == 415
            assert binary.json()["detail"] == attachment_rules.UNSUPPORTED

    asyncio.run(body())


def test_a_message_carries_pdf_and_text_with_their_names(make_backend, upstream) -> None:
    upstream.script = [{"content": "收到。"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            pdf = await upload(client, PDF, "application/pdf", "报告.pdf")
            text = await upload(client, b"print('hi')\n", "text/plain", "script.py")
            run = await send(client, direct_id, "看看这两份", [pdf["id"], text["id"]])
            assert run["status"] == "completed", run

            user = next(m for m in (await get_conversation(client, direct_id))["messages"] if m["kind"] == "user")
            carried = {item["id"]: item for item in user["attachments"]}
            assert carried[pdf["id"]]["name"] == "报告.pdf"
            assert carried[pdf["id"]]["mime"] == "application/pdf"
            assert carried[text["id"]]["name"] == "script.py"
            assert carried[text["id"]]["mime"] == "text/plain"

    asyncio.run(body())


def test_a_text_file_is_inline_for_every_model_and_says_how_much_was_cut(
    make_backend, upstream
) -> None:
    upstream.script = [{"content": "读完了。"}]
    backend = make_backend()
    long_body = "a" * (attachment_rules.TEXT_FILE_CHARS + 120)

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            short = await upload(client, b"const answer = 42;\n", "text/plain", "answer.ts")
            long = await upload(client, long_body.encode(), "text/plain", "big.txt")
            run = await send(client, direct_id, "读文件", [short["id"], long["id"]])
            assert run["status"] == "completed", run

        body_text = texts(upstream.requests[0])
        assert f"[文本文件 answer.ts（{short['id']}）]" in body_text
        assert "const answer = 42;" in body_text
        assert f"[文本文件 big.txt（{long['id']}）]" in body_text
        assert f"共 {len(long_body)} 字符" in body_text
        assert "a" * attachment_rules.TEXT_FILE_CHARS in body_text
        # The tail past the cap is what got dropped.
        assert "a" * (attachment_rules.TEXT_FILE_CHARS + 1) not in body_text

    asyncio.run(body())


def test_history_hands_pdfs_to_models_that_read_them_and_labels_for_the_rest(tmp_path) -> None:
    directory = tmp_path / "attachments"
    rows = []
    attachments = []
    for index, name in enumerate(("最早.pdf", "中间.pdf", "最新.pdf"), start=1):
        data = PDF + bytes([index])
        sha = attachment_rules.write_file(directory, data)
        attachment = {
            "id": f"att_{index:012x}",
            "mime": "application/pdf",
            "size": len(data),
            "sha256": sha,
            "name": name,
        }
        attachments.append(attachment)
        rows.append(
            {
                "id": f"msg_{index}",
                "kind": "user",
                "seq": index,
                "content": name,
                "status": "complete",
                "author_id": None,
                "author_name": "你",
                "attachments": [attachment],
            }
        )

    reading = history_rules.to_chat_history(
        rows, self_id="agent", vision=False, pdf=True, attachments_dir=directory
    )
    files = [part["file"] for message in reading for part in message["content"] if part.get("type") == "file"]
    # Only the newest two travel as documents, newest last; the oldest is a label.
    assert [item["filename"] for item in files] == ["中间.pdf", "最新.pdf"]
    assert files[0]["file_data"].startswith("data:application/pdf;base64,")
    assert "这里读不到内容" in str(reading[0]["content"])

    blind = history_rules.to_chat_history(
        rows, self_id="agent", vision=False, pdf=False, attachments_dir=directory
    )
    assert all(
        part.get("type") != "file"
        for message in blind
        for part in message["content"]
    )
    assert "这里读不到内容" in str(blind[-1]["content"])


def test_a_v23_database_keeps_its_attachments_and_gains_a_name(make_backend) -> None:
    backend = make_backend()
    store = store_of(backend)

    async def body() -> str:
        async with client_for(backend) as client:
            return (await upload(client, PNG))["id"]

    attachment_id = asyncio.run(body())
    # Rewind the schema to the version before names existed, dropping the column with it.
    with store._transaction() as tx:  # noqa: SLF001 - ageing the schema is the point here
        tx.execute("ALTER TABLE attachments DROP COLUMN name")
        tx.execute("UPDATE meta SET value = '23' WHERE key = 'schema_version'")
    backend.restart()
    store = store_of(backend)

    kept = store.get_attachment(attachment_id)
    assert kept is not None
    assert kept["name"] == ""
    assert kept["mime"] == "image/png"
    assert store._schema_version(store._ensure()) == SCHEMA_VERSION  # noqa: SLF001
