"""Agent skills: parser rules, the HTTP API, the SSE event, the tools and the prompt block."""

from __future__ import annotations

import asyncio
import io
import zipfile

import httpx
import pytest

from app import skills as skills_rules
from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    collect_events,
    event_names,
    first_payload,
    get_workspace,
    last_user_text,
    payloads,
    run_goal,
    store_of,
    system_prompt,
    tool_results,
)


# ---------------------------------------------------------------------- parser


def test_parser_accepts_spec_examples() -> None:
    parsed = skills_rules.parse_skill_markdown(
        "---\nname: weekly-report\ndescription: 每周整理团队周报\n---\n\n# 做法\n1. 读数据\n"
    )
    assert parsed["name"] == "weekly-report"
    assert parsed["description"] == "每周整理团队周报"
    assert parsed["body"] == "# 做法\n1. 读数据"

    folded = skills_rules.parse_skill_markdown(
        "---\n"
        "name: deploy\n"
        "description: >\n"
        "  把代码部署到\n"
        "  服务器上\n"
        "metadata:\n"
        "  lang: python\n"
        "---\n"
        "步骤\n"
    )
    assert folded["name"] == "deploy"
    assert folded["description"] == "把代码部署到 服务器上"
    assert folded["metadata"] == {"lang": "python"}
    assert folded["body"] == "步骤"

    quoted = skills_rules.parse_skill_markdown(
        '---\nname: demo\ndescription: "带引号的描述"\n---\n正文\n'
    )
    assert quoted["description"] == "带引号的描述"


def test_parser_reads_hyphenated_keys_real_skills_use() -> None:
    # Verbatim shape of vercel's web-design-guidelines, as Claude Code and Codex install it.
    parsed = skills_rules.parse_skill_markdown(
        "---\n"
        "name: web-design-guidelines\n"
        "description: Review UI code.\n"
        "allowed-tools: Read, Grep\n"
        "metadata:\n"
        "  author: vercel\n"
        '  version: "1.0.0"\n'
        "  argument-hint: <file-or-pattern>\n"
        "---\n"
        "# Web Interface Guidelines\n"
    )
    assert parsed["metadata"] == {
        "author": "vercel",
        "version": "1.0.0",
        "argument-hint": "<file-or-pattern>",
    }


def test_a_slash_name_in_the_message_tells_the_bot_to_use_that_skill(make_backend, upstream) -> None:
    backend = make_backend()
    upstream.script = [{"content": "好"}, {"content": "好"}]

    async def body() -> None:
        async with client_for(backend) as client:
            for name, enabled in (("weekly-report", True), ("old-way", False)):
                created = (
                    await client.post("/skills", json={"name": name, "description": "做法", "body": "步骤"})
                ).json()
                if not enabled:
                    await client.patch(f"/skills/{created['id']}", json={"enabled": False})
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            await run_goal(client, direct_id, "/weekly-report 写这周的周报，参考 a/old-way 和 /old-way")
            await run_goal(client, direct_id, "路径 docs/weekly-report 不算点名")

        called = last_user_text(upstream.requests[0])
        assert "/weekly-report 点名了技能「weekly-report」：先调用 use_skill" in called
        assert "/old-way 点名了技能「old-way」，但你现在用不了它" in called
        assert called.count("点名了技能") == 2
        assert "点名了技能" not in last_user_text(upstream.requests[1])

    asyncio.run(body())


def test_parser_rejects_bad_names_descriptions_and_missing_frontmatter() -> None:
    for bad in ("Bad-Name", "bad--name", "-bad", "bad-", "bad_name", "a" * 65):
        with pytest.raises(skills_rules.SkillInvalid):
            skills_rules.parse_skill_markdown(
                f"---\nname: {bad}\ndescription: 描述\n---\n正文\n"
            )
    with pytest.raises(skills_rules.SkillInvalid):
        skills_rules.parse_skill_markdown("---\nname: demo\n---\n正文\n")  # no description
    with pytest.raises(skills_rules.SkillInvalid):
        skills_rules.parse_skill_markdown("---\nname: demo\ndescription:   \n---\n正文\n")
    with pytest.raises(skills_rules.SkillInvalid):
        skills_rules.parse_skill_markdown("没有 frontmatter\n正文\n")


# ---------------------------------------------------------------------- HTTP


def test_post_creates_enabled_user_skill_and_writes_skill_md(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = await client.post(
                "/skills",
                json={"name": "weekly-report", "description": "每周整理周报", "body": "# 做法"},
            )
            assert created.status_code == 201, created.text
            data = created.json()
            assert data["name"] == "weekly-report"
            assert data["source"] == "user"
            assert data["enabled"] is True
            assert data["has_scripts"] is False
            assert data["file_count"] == 1
            assert data["body"] == "# 做法"
            assert data["files"] == []

            store = store_of(backend)
            path = store.skills_dir / "weekly-report" / "SKILL.md"
            assert path.exists()
            text = path.read_text(encoding="utf-8")
            assert "name: weekly-report" in text
            assert "# 做法" in text

            # A taken name is a 409.
            dup = await client.post(
                "/skills", json={"name": "weekly-report", "description": "x", "body": ""}
            )
            assert dup.status_code == 409, dup.text

            # A bad name is a 422.
            bad = await client.post(
                "/skills", json={"name": "Bad-Name", "description": "x", "body": ""}
            )
            assert bad.status_code == 422, bad.text

    asyncio.run(body())


def _zip_skill(name: str, description: str, body: str, files: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
        )
        for relative, content in (files or {}).items():
            archive.writestr(f"{name}/{relative}", content)
    return buf.getvalue()


def test_upload_zip_lands_disabled_with_scripts(make_backend) -> None:
    backend = make_backend()
    payload = _zip_skill(
        "data-tool", "处理数据", "做法", {"scripts/extract.py": "print('hi')", "references/api.md": "API 文档"}
    )

    async def body() -> None:
        async with client_for(backend) as client:
            response = await client.post(
                "/skills/upload",
                content=payload,
                headers={"Content-Type": "application/zip"},
            )
            assert response.status_code == 201, response.text
            data = response.json()
            assert data["name"] == "data-tool"
            assert data["source"] == "upload"
            assert data["enabled"] is False
            assert data["has_scripts"] is True
            assert data["file_count"] == 3
            assert [f["path"] for f in data["files"]] == ["references/api.md", "scripts/extract.py"]

            store = store_of(backend)
            assert (store.skills_dir / "data-tool" / "scripts" / "extract.py").exists()

    asyncio.run(body())


def test_upload_zip_rejects_traversal_and_oversize(make_backend) -> None:
    backend = make_backend()

    def traversal_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("../evil.txt", "boom")
        return buf.getvalue()

    def oversize_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("skill/SKILL.md", "---\nname: skill\ndescription: 描述\n---\n正文")
            archive.writestr("skill/big.bin", b"0" * (3 * 1024 * 1024))
        return buf.getvalue()

    async def body() -> None:
        async with client_for(backend) as client:
            bad = await client.post(
                "/skills/upload", content=traversal_zip(), headers={"Content-Type": "application/zip"}
            )
            assert bad.status_code == 422, bad.text

            big = await client.post(
                "/skills/upload", content=oversize_zip(), headers={"Content-Type": "application/zip"}
            )
            assert big.status_code == 413, big.text

    asyncio.run(body())


def test_github_import_lands_disabled(make_backend, upstream) -> None:
    backend = make_backend()
    payload = _zip_skill("gh-skill", "从 GitHub 导入", "做法", {"scripts/run.sh": "echo hi"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async def body() -> None:
        backend.app.state.engine.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        async with client_for(backend) as client:
            response = await client.post(
                "/skills/github", json={"url": "https://github.com/owner/repo"}
            )
            assert response.status_code == 201, response.text
            data = response.json()
            assert data["source"] == "github"
            assert data["enabled"] is False
            assert data["has_scripts"] is True
            assert data["source_url"] == "https://github.com/owner/repo"

    asyncio.run(body())


def _local_skill(root, name: str, description: str, extra: dict[str, str] | None = None):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n做法\n")
    for path, text in (extra or {}).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_text(text)
    return folder


def test_local_skills_are_listed_once_and_import_disabled(make_backend, tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    library = tmp_path / "library"
    shared = _local_skill(library, "shared-skill", "两个应用共用的技能")
    claude = home / ".claude" / "skills"
    _local_skill(claude, "review-ui", "检查界面", {"scripts/check.sh": "echo ok", ".git/HEAD": "x"})
    _local_skill(claude, "bad-name", "名字和文件夹不一致").joinpath("SKILL.md").write_text(
        "---\nname: other\ndescription: x\n---\n"
    )
    (claude / "shared-skill").symlink_to(shared)
    codex = home / ".codex" / "skills"
    codex.mkdir(parents=True)
    (codex / "shared-skill").symlink_to(shared)
    monkeypatch.setenv("HOME", str(home))
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            listed = (await client.get("/skills/local")).json()["skills"]
            rows = {(row["app"], row["name"]): row for row in listed}
            # The shared library skill shows once, under the first app that links it.
            assert sorted(rows) == [
                ("Claude Code", "bad-name"),
                ("Claude Code", "review-ui"),
                ("Claude Code", "shared-skill"),
            ]
            assert "不一致" in rows[("Claude Code", "bad-name")]["error"]
            review = rows[("Claude Code", "review-ui")]
            assert (review["description"], review["installed"], review["error"]) == ("检查界面", False, None)

            created = await client.post("/skills/local", json={"path": review["path"]})
            assert created.status_code == 201, created.text
            data = created.json()
            assert (data["source"], data["enabled"], data["has_scripts"]) == ("local", False, True)
            assert [f["path"] for f in data["files"]] == ["scripts/check.sh"]

            linked = await client.post(
                "/skills/local", json={"path": rows[("Claude Code", "shared-skill")]["path"]}
            )
            assert linked.status_code == 201, linked.text

            again = (await client.get("/skills/local")).json()["skills"]
            assert {row["name"]: row["installed"] for row in again}["review-ui"] is True
            assert (await client.post("/skills/local", json={"path": review["path"]})).status_code == 409

    asyncio.run(body())


def test_local_import_reads_only_listed_folders(make_backend, tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = home / ".claude" / "skills"
    outside = _local_skill(tmp_path / "elsewhere", "secret", "不在技能目录里")
    linked = _local_skill(root, "has-link", "里面有链接")
    (linked / "passwd").symlink_to("/etc/passwd")
    monkeypatch.setenv("HOME", str(home))
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            for path in (str(outside), str(root / "has-link" / "scripts"), str(root / ".." / "skills" / "has-link")):
                refused = await client.post("/skills/local", json={"path": path})
                assert refused.status_code == 422, (path, refused.text)
            link = await client.post("/skills/local", json={"path": str(root / "has-link")})
            assert link.status_code == 422 and "链接" in link.json()["detail"]

    asyncio.run(body())


def test_patch_enable_and_edit_rewrites_file(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            created = (
                await client.post(
                    "/skills", json={"name": "notes", "description": "记笔记", "body": "旧做法"}
                )
            ).json()

            patched = await client.patch(
                f"/skills/{created['id']}", json={"body": "新做法", "description": "记更好的笔记"}
            )
            assert patched.status_code == 200, patched.text
            data = patched.json()
            assert data["body"] == "新做法"
            assert data["description"] == "记更好的笔记"
            assert data["enabled"] is True

            store = store_of(backend)
            text = (store.skills_dir / "notes" / "SKILL.md").read_text(encoding="utf-8")
            assert "记更好的笔记" in text
            assert "新做法" in text

            disabled = await client.patch(f"/skills/{created['id']}", json={"enabled": False})
            assert disabled.json()["enabled"] is False

    asyncio.run(body())


def test_delete_removes_folder_and_prunes_skills_disabled(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            supervisor = next(a for a in workspace["agents"] if a["kind"] == "supervisor")
            skill = (
                await client.post(
                    "/skills", json={"name": "gone", "description": "会被删掉", "body": "x"}
                )
            ).json()
            # Opt the supervisor out of this skill.
            patched = await client.patch(
                f"/agents/{supervisor['id']}",
                json={
                    "tool_permissions": {
                        "code": True,
                        "files": True,
                        "computer": True,
                        "connectors": "all",
                        "skills_disabled": ["gone"],
                    }
                },
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["tool_permissions"]["skills_disabled"] == ["gone"]

            deleted = await client.delete(f"/skills/{skill['id']}")
            assert deleted.status_code == 204

            store = store_of(backend)
            assert not (store.skills_dir / "gone").exists()
            assert store.get_skill(skill["id"]) is None
            # The name was pruned from the supervisor's opt-out list.
            refreshed = next(a for a in (await get_workspace(client))["agents"] if a["id"] == supervisor["id"])
            assert refreshed["tool_permissions"]["skills_disabled"] == []

    asyncio.run(body())


def test_a_change_publishes_a_skills_event(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            reader = asyncio.create_task(
                collect_events(
                    client,
                    "/events",
                    until=lambda events: any(name == "skills" for name, _ in events),
                    timeout=15,
                )
            )
            await asyncio.sleep(0.2)
            created = await client.post(
                "/skills", json={"name": "evented", "description": "触发事件", "body": "x"}
            )
            assert created.status_code == 201
            events = await reader
            published = first_payload(events, "skills")
            assert [row["name"] for row in published["skills"]] == ["evented"]

    asyncio.run(body())


# ---------------------------------------------------------------------- tools / prompt


def test_bot_sees_skills_block_and_use_skill_bumps_use_count(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            supervisor_id = store.supervisor()["id"]
            for name, description in (
                ("a-skill", "可用的技能"),
                ("b-skill", "停用的技能"),
                ("c-skill", "被禁用的技能"),
            ):
                response = await client.post(
                    "/skills", json={"name": name, "description": description, "body": f"{name} 的做法"}
                )
                assert response.status_code == 201, response.text
            b_skill = (await client.get("/skills")).json()["skills"]
            b_id = next(s["id"] for s in b_skill if s["name"] == "b-skill")
            await client.patch(f"/skills/{b_id}", json={"enabled": False})
            await client.patch(
                f"/agents/{supervisor_id}",
                json={
                    "tool_permissions": {
                        "code": True,
                        "files": True,
                        "computer": True,
                        "connectors": "all",
                        "skills_disabled": ["c-skill"],
                    }
                },
            )

            upstream.script = [
                tool_calls(call("call_1", "use_skill", {"name": "a-skill"})),
                {"content": "完成"},
            ]
            workspace = await get_workspace(client)
            run = await run_goal(client, workspace["direct_conversation_id"], "用一下技能")
            assert run["status"] == "completed", run

            system = system_prompt(upstream.requests[0])
            assert "<skills>" in system
            assert "- a-skill：可用的技能" in system
            assert "b-skill" not in system
            assert "c-skill" not in system

            results = tool_results(upstream.requests[1])
            used = next((r for _, r in results if r.get("name") == "a-skill"), None)
            assert used is not None, results
            assert used["ok"] is True
            assert used["body"] == "a-skill 的做法"
            assert used["has_scripts"] is False

            listed = {s["name"]: s for s in (await client.get("/skills")).json()["skills"]}
            assert listed["a-skill"]["use_count"] == 1
            assert listed["a-skill"]["last_used_at"] is not None
            assert listed["b-skill"]["use_count"] == 0

    asyncio.run(body())


def test_skill_create_from_model_tool_call_is_agent_sourced_and_disabled(make_backend, upstream) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            upstream.script = [
                tool_calls(
                    call(
                        "call_1",
                        "skill_create",
                        {"name": "model-written", "description": "模型写的做法", "body": "第一步"},
                    )
                ),
                {"content": "写好了"},
            ]
            workspace = await get_workspace(client)
            run = await run_goal(client, workspace["direct_conversation_id"], "写一个技能")
            assert run["status"] == "completed", run

            skills = (await client.get("/skills")).json()["skills"]
            created = next(s for s in skills if s["name"] == "model-written")
            assert created["source"] == "agent"
            assert created["enabled"] is False
            assert created["author_id"] == workspace["agents"][0]["id"]

    asyncio.run(body())


def test_skills_disabled_roundtrips_and_old_payloads_still_validate(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            supervisor = next(a for a in workspace["agents"] if a["kind"] == "supervisor")

            # An explicit list round-trips.
            patched = await client.patch(
                f"/agents/{supervisor['id']}",
                json={
                    "tool_permissions": {
                        "code": True,
                        "files": True,
                        "computer": True,
                        "connectors": "all",
                        "skills_disabled": ["one", "two"],
                    }
                },
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["tool_permissions"]["skills_disabled"] == ["one", "two"]

            # A payload without the key (an older client) still validates and defaults to [].
            old = await client.patch(
                f"/agents/{supervisor['id']}",
                json={
                    "tool_permissions": {
                        "code": True,
                        "files": True,
                        "computer": True,
                        "connectors": "all",
                    }
                },
            )
            assert old.status_code == 200, old.text
            assert old.json()["tool_permissions"]["skills_disabled"] == []

    asyncio.run(body())
