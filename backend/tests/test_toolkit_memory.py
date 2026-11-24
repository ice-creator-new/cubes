"""Memory and workbench pack: scoped history search, conversation reads, notes and tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.store import Store
from app.toolkit import split_result, tool_specs
from app.toolkit.base import ToolContext, ToolInputError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def seeded_store(tmp_path: Path) -> tuple[Store, dict[str, Any]]:
    """A supervisor, three members, a group, one private pair and a decoy direct chat."""
    store = Store(tmp_path / "cubes.sqlite3")
    store.initialize()
    supervisor = store.supervisor()
    assert supervisor is not None
    members = [
        store.create_agent(name=name, instructions="测试用成员", kind="member")
        for name in ("阿甲", "阿乙", "阿丙")
    ]
    a, b, c = members
    direct = store.ensure_direct_conversation(supervisor["id"])
    assert direct is not None
    group = store.create_conversation(
        kind="group",
        title="团队群",
        purpose="一起干活",
        member_ids=[supervisor["id"], a["id"], b["id"]],
    )
    private = store.create_conversation(
        kind="private",
        title="阿甲 ⇄ 阿乙",
        purpose="两人的私聊",
        member_ids=[a["id"], b["id"]],
    )
    other = store.create_conversation(
        kind="direct", title="阿丙", purpose="与「阿丙」的私聊", member_ids=[c["id"]]
    )

    def say(conversation: dict, author: dict, content: str, kind: str = "agent") -> None:
        store.append_message(
            conversation_id=conversation["id"],
            author_id=author["id"],
            author_name=author["name"],
            avatar_seed=author["avatar_seed"],
            kind=kind,
            content=content,
            status="complete",
        )

    say(direct, supervisor, "Tauri 的毛玻璃材质在主管私聊里说过了。", kind="user")
    say(group, a, "Tauri 毛玻璃在群里的方案。")
    say(private, a, "我们私下聊聊 Tauri 毛玻璃。")
    say(other, c, "阿丙也在想 Tauri 毛玻璃。")
    return store, {
        "supervisor": supervisor,
        "a": a,
        "b": b,
        "c": c,
        "direct": direct,
        "group": group,
        "private": private,
        "other": other,
    }


def context_for(
    store: Store, agent: dict, published: list[Any] | None = None, tmp_path: Path | None = None
) -> ToolContext:
    settings = Settings(database_path=store.path)
    return ToolContext(
        store=store,
        settings=settings,
        run_id="r1",
        conversation_id="c1",
        agent=agent,
        http=httpx.AsyncClient(trust_env=False),
        publish=lambda event, payload: (published if published is not None else []).append(
            (event, payload)
        ),
    )


def spec_named(store: Store, name: str):
    settings = Settings(database_path=store.path)
    return next(spec for spec in tool_specs(settings, "supervisor") if spec.name == name)


async def call_tool(store: Store, agent: dict, name: str, args: dict, published=None) -> str:
    context = context_for(store, agent, published, store.path.parent)
    spec = spec_named(store, name)
    try:
        text, _ = split_result(await spec.run(context, args))
        return text
    finally:
        await context.http.aclose()


# ------------------------------------------------------------------ history


def test_supervisor_searches_every_conversation_it_belongs_to(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)

    async def body() -> None:
        text = await call_tool(store, data["supervisor"], "search_history", {"query": "毛玻璃"})
        # The supervisor reads every direct and group thread, private pairs stay hidden.
        assert "找到 3 条相关消息：" in text
        assert "「主管」" in text
        assert "「团队群」" in text
        assert "「阿丙」" in text
        assert "阿甲 ⇄ 阿乙" not in text
        assert "毛玻璃" in text

    run(body())
    store.close()


def test_member_sees_group_and_its_private_but_not_other_threads(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)

    async def body() -> None:
        text = await call_tool(store, data["a"], "search_history", {"query": "毛玻璃"})
        assert "「团队群」" in text
        assert "阿甲 ⇄ 阿乙" in text
        assert "「主管」" not in text
        assert "阿丙" not in text
        assert "找到 2 条相关消息：" in text

    run(body())
    store.close()


def test_snippet_surrounds_the_match(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)
    padding = "铺垫" * 100
    store.append_message(
        conversation_id=data["group"]["id"],
        author_id=data["a"]["id"],
        author_name="阿甲",
        avatar_seed=data["a"]["avatar_seed"],
        kind="agent",
        content=f"{padding}关键结论在这里{padding}",
        status="complete",
    )

    async def body() -> None:
        text = await call_tool(store, data["b"], "search_history", {"query": "关键结论"})
        assert "关键结论" in text
        snippet = [line for line in text.splitlines() if "关键结论" in line][0]
        assert "…" in snippet
        assert len(snippet) < 400

    run(body())
    store.close()


def test_search_history_validates_input(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)

    async def body() -> None:
        for args in ({"query": "  "}, {"query": "毛玻璃", "limit": 0}, {"query": "x", "limit": 99}):
            try:
                await call_tool(store, data["a"], "search_history", args)
            except ToolInputError as exc:
                assert str(exc)
            else:  # pragma: no cover - the loop must raise every time
                raise AssertionError(f"{args} should fail")

    run(body())
    store.close()


def test_read_conversation_enforces_the_same_scope(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)

    async def body() -> None:
        allowed = await call_tool(
            store, data["a"], "read_conversation", {"conversation_id": data["private"]["id"]}
        )
        assert "阿甲 ⇄ 阿乙" in allowed
        assert "我们私下聊聊" in allowed
        own = await call_tool(
            store, data["c"], "read_conversation", {"conversation_id": data["other"]["id"]}
        )
        assert "阿丙也在想" in own
        forbidden = [
            (data["c"], data["private"]["id"]),
            (data["a"], data["direct"]["id"]),
        ]
        for agent, conversation_id in forbidden:
            try:
                await call_tool(
                    store, agent, "read_conversation", {"conversation_id": conversation_id}
                )
            except ToolInputError as exc:
                assert "会话" in str(exc)
            else:  # pragma: no cover
                raise AssertionError(f"{conversation_id} should be out of scope")
        try:
            await call_tool(store, data["a"], "read_conversation", {"conversation_id": "nope"})
        except ToolInputError as exc:
            assert "不存在" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("unknown conversation should fail")

    run(body())
    store.close()


def test_read_conversation_is_capped(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)
    for index in range(30):
        store.append_message(
            conversation_id=data["group"]["id"],
            author_id=data["a"]["id"],
            author_name="阿甲",
            avatar_seed=data["a"]["avatar_seed"],
            kind="agent",
            content=f"第{index}条",
            status="complete",
        )

    async def body() -> None:
        text = await call_tool(
            store, data["b"], "read_conversation", {"conversation_id": data["group"]["id"], "limit": 5}
        )
        assert "第29条" in text
        assert "第25条" in text
        assert "第24条" not in text

    run(body())
    store.close()


def test_note_preview_strips_markdown() -> None:
    from app.toolkit.workbench import note_preview

    preview = note_preview(
        "## 结论\n\n| 段落 | 上限 |\n| --- | --- |\n| 三段 | 12 |\n\n"
        "- 第一点\n- 第二点\n\n`code` 与 **重点**\n[来源](https://example.com/x)\n```py\nx = 1\n```"
    )
    for marker in ("#", "|", "`", "**", "[", "]", "https://", "---"):
        assert marker not in preview, preview
    assert "结论" in preview
    assert "三段" in preview
    assert "第一点" in preview
    assert "重点" in preview
    assert "来源" in preview
    assert "第一点 第二点" in preview


# -------------------------------------------------------------- notes and tasks


def test_notes_round_trip_and_publish(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)
    published: list[Any] = []

    async def body() -> None:
        created = await call_tool(
            store,
            data["a"],
            "note_create",
            {"title": "调研结论", "content": "第一行\n第二行内容"},
            published,
        )
        assert "调研结论" in created
        notes = store.list_notes()
        assert len(notes) == 1
        note_id = notes[0]["id"]
        assert note_id in created
        assert published[-1][0] == "notes"
        assert published[-1][1]["notes"][0]["title"] == "调研结论"
        assert published[-1][1]["notes"][0]["preview"].startswith("第一行")

        read = await call_tool(store, data["b"], "note_read", {"note_id": note_id})
        assert "第一行" in read and "第二行内容" in read

        updated = await call_tool(
            store, data["b"], "note_update", {"note_id": note_id, "content": "改过的正文"}, published
        )
        assert "改过的正文" in updated
        assert "第二行内容" not in store.get_note(note_id)["content"]
        assert published[-1][0] == "notes"
        assert published[-1][1]["notes"][0]["preview"] == "改过的正文"

        listed = await call_tool(store, data["a"], "note_list", {"query": "调研"})
        assert "调研结论" in listed
        everything = await call_tool(store, data["a"], "note_list", {})
        assert everything.count("调研结论") == 1

        try:
            await call_tool(store, data["a"], "note_create", {"title": "  ", "content": "x"})
        except ToolInputError as exc:
            assert "标题" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("blank title should fail")
        try:
            await call_tool(store, data["a"], "note_update", {"note_id": "n_missing", "content": "x"})
        except ToolInputError as exc:
            assert "笔记" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing note should fail")
        try:
            await call_tool(store, data["a"], "note_read", {"note_id": "n_missing"})
        except ToolInputError as exc:
            assert "笔记" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing note should fail")

    run(body())
    store.close()


def test_tasks_round_trip_and_publish(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)
    published: list[Any] = []

    async def body() -> None:
        created = await call_tool(
            store,
            data["supervisor"],
            "task_create",
            {"title": "写调研报告", "detail": "覆盖三家竞品", "assignee_id": data["a"]["id"]},
            published,
        )
        assert "写调研报告" in created
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "todo"
        assert tasks[0]["assignee_id"] == data["a"]["id"]
        task_id = tasks[0]["id"]
        assert published[-1][0] == "tasks"
        assert published[-1][1]["tasks"][0]["title"] == "写调研报告"

        moved = await call_tool(
            store, data["a"], "task_update", {"task_id": task_id, "status": "doing", "note": "已经开始"}, published
        )
        assert "进行中" in moved
        assert store.get_task(task_id)["status"] == "doing"
        assert store.get_task(task_id)["note"] == "已经开始"
        assert published[-1][0] == "tasks"

        listed = await call_tool(store, data["a"], "task_list", {"status": "doing"})
        assert "写调研报告" in listed
        empty = await call_tool(store, data["a"], "task_list", {"status": "done"})
        assert "写调研报告" not in empty

        for args in (
            {"title": "  "},
            {"title": "x", "assignee_id": "a_missing"},
        ):
            try:
                await call_tool(store, data["a"], "task_create", args)
            except ToolInputError:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"{args} should fail")
        try:
            await call_tool(store, data["a"], "task_update", {"task_id": task_id, "status": "someday"})
        except ToolInputError as exc:
            assert "状态" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("bad status should fail")
        try:
            await call_tool(store, data["a"], "task_update", {"task_id": "t_missing", "status": "done"})
        except ToolInputError as exc:
            assert "任务" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("missing task should fail")

    run(body())
    store.close()


# ------------------------------------------------------------------- REST


def test_notes_and_tasks_rest_shapes(make_backend) -> None:
    backend = make_backend()
    store = backend.app.state.store
    supervisor = store.supervisor()

    async def body() -> None:
        store.create_note(title="接口笔记", content="正文第一段\n正文第二段", author_id=supervisor["id"])
        store.create_task(
            title="接口任务",
            detail="细节",
            assignee_id=supervisor["id"],
            author_id=supervisor["id"],
        )
        async with httpx.AsyncClient(base_url=backend.url, timeout=10.0, trust_env=False) as client:
            notes = (await client.get("/notes")).json()
            assert set(notes) == {"notes"}
            entry = notes["notes"][0]
            assert set(entry) == {"id", "title", "preview", "author_id", "updated_at"}
            assert entry["title"] == "接口笔记"
            assert entry["preview"] == "正文第一段 正文第二段"
            full = (await client.get(f"/notes/{entry['id']}")).json()
            assert full["content"] == "正文第一段\n正文第二段"
            assert full["author_id"] == supervisor["id"]
            assert set(full) >= {"id", "title", "content", "author_id", "created_at", "updated_at"}
            missing = await client.get("/notes/n_missing")
            assert missing.status_code == 404
            assert missing.json()["detail"].endswith("。")
            tasks = (await client.get("/tasks")).json()
            assert set(tasks) == {"tasks"}
            task = tasks["tasks"][0]
            assert set(task) == {
                "id",
                "title",
                "detail",
                "status",
                "assignee_id",
                "author_id",
                "note",
                "created_at",
                "updated_at",
            }
            assert task["status"] == "todo"

    run(body())


def test_note_delete_removes_and_publishes(tmp_path: Path) -> None:
    store, data = seeded_store(tmp_path)
    published: list[Any] = []
    note = store.create_note(title="过期结论", content="旧数据", author_id=data["a"]["id"])

    async def body() -> None:
        args = {"note_id": note["id"], "reason": "数据已经不成立"}
        text = await call_tool(store, data["b"], "note_delete", args, published)
        assert "已删除笔记「过期结论」" in text
        assert "数据已经不成立" in text
        assert store.get_note(note["id"]) is None
        assert store.list_notes() == []
        assert published[-1][0] == "notes"
        assert published[-1][1] == {"notes": []}
        # The summary is resolved after the run, so it carries the title it just looked up.
        assert spec_named(store, "note_delete").summary(args) == "删除笔记「过期结论」"

        try:
            await call_tool(store, data["a"], "note_delete", {"note_id": note["id"]})
        except ToolInputError as exc:
            assert str(exc) == f"笔记「{note['id']}」不存在。"
        else:  # pragma: no cover
            raise AssertionError("missing note should fail")
        missing = spec_named(store, "note_delete")
        assert missing.summary({"note_id": "n_missing"}) == "删除笔记「n_missing」"

    run(body())
    store.close()


def test_note_delete_rest_returns_204_and_publishes(make_backend) -> None:
    from helpers import collect_events, event_names, payloads

    backend = make_backend()
    store = backend.app.state.store
    supervisor = store.supervisor()
    note = store.create_note(title="待删笔记", content="没有人再用了", author_id=supervisor["id"])

    async def body() -> None:
        async with httpx.AsyncClient(base_url=backend.url, timeout=10.0, trust_env=False) as client:
            async with httpx.AsyncClient(base_url=backend.url, timeout=10.0, trust_env=False) as reader:
                stream = asyncio.create_task(
                    collect_events(reader, "/events", duration=1.2)
                )
                await asyncio.sleep(0.25)
                response = await client.delete(f"/notes/{note['id']}")
                assert response.status_code == 204
                assert response.content == b""
                assert (await client.get("/notes")).json() == {"notes": []}
                gone = await client.get(f"/notes/{note['id']}")
                assert gone.status_code == 404
                assert gone.json()["detail"] == "笔记不存在。"
                assert store.get_note(note["id"]) is None
                twice = await client.delete(f"/notes/{note['id']}")
                assert twice.status_code == 404
                assert twice.json()["detail"] == "笔记不存在。"
                events = await stream
        assert "notes" in event_names(events)
        assert payloads(events, "notes")[-1] == {"notes": []}

    run(body())


def test_task_patch_updates_status_note_and_publishes(make_backend) -> None:
    from helpers import collect_events, event_names, payloads

    backend = make_backend()
    store = backend.app.state.store
    supervisor = store.supervisor()
    task = store.create_task(
        title="写周报", detail="覆盖本周成果", assignee_id=supervisor["id"], author_id=supervisor["id"]
    )

    async def body() -> None:
        async with httpx.AsyncClient(base_url=backend.url, timeout=10.0, trust_env=False) as client:
            async with httpx.AsyncClient(base_url=backend.url, timeout=10.0, trust_env=False) as reader:
                stream = asyncio.create_task(
                    collect_events(
                        reader,
                        "/events",
                        until=lambda events: any(
                            name == "tasks"
                            and payload["tasks"][0]["status"] == "done"
                            for name, payload in events
                        ),
                    )
                )
                await asyncio.sleep(0.25)
                response = await client.patch(f"/tasks/{task['id']}", json={"status": "done"})
                assert response.status_code == 200, response.text
                updated = response.json()
                assert updated["id"] == task["id"]
                assert updated["status"] == "done"
                assert updated["note"] == ""
                assert set(updated) == {
                    "id",
                    "title",
                    "detail",
                    "status",
                    "assignee_id",
                    "author_id",
                    "note",
                    "created_at",
                    "updated_at",
                }
                assert store.get_task(task["id"])["status"] == "done"

                noted = (await client.patch(f"/tasks/{task['id']}", json={"note": "  这周做完了  "})).json()
                assert noted["note"] == "这周做完了"
                assert noted["status"] == "done"

                cleared = (await client.patch(f"/tasks/{task['id']}", json={"note": ""})).json()
                assert cleared["note"] == ""

                back = (await client.patch(f"/tasks/{task['id']}", json={"status": "todo"})).json()
                assert back["status"] == "todo"

                missing = await client.patch("/tasks/t_missing", json={"status": "done"})
                assert missing.status_code == 404
                assert missing.json()["detail"] == "任务不存在。"

                bad_status = await client.patch(f"/tasks/{task['id']}", json={"status": "someday"})
                assert bad_status.status_code == 422
                detail = bad_status.json()["detail"]
                assert detail.startswith("status：")
                assert detail.endswith("。")
                assert "todo" in detail

                too_long = await client.patch(f"/tasks/{task['id']}", json={"note": "长" * 4001})
                assert too_long.status_code == 422
                assert "4000" in too_long.json()["detail"]

                empty = await client.patch(f"/tasks/{task['id']}", json={})
                assert empty.status_code == 422
                assert "至少需要提供" in empty.json()["detail"]

                extra = await client.patch(f"/tasks/{task['id']}", json={"status": "done", "title": "x"})
                assert extra.status_code == 422

                events = await stream
        assert "tasks" in event_names(events)
        assert payloads(events, "tasks")[0]["tasks"][0]["status"] == "done"
        # The store ends where the last hand edit left it.
        assert store.get_task(task["id"])["status"] == "todo"

    run(body())
