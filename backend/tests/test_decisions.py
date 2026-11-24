"""The sidebar's waiting list: one row per card or workflow question."""

from __future__ import annotations

from app import workflows as rules
from app.decisions import collect_decisions, decision_rows
from app.store import Store


def test_each_card_says_what_silence_does() -> None:
    rows = decision_rows(
        actions=[
            _action("x_del", "delete_agent", "主管想删除成员「甲」。", "2026-11-06T00:00:01Z"),
            _action("x_group", "dissolve_group", "主管想解散群组「周会」。", "2026-11-06T00:00:02Z"),
            _action("x_budget", "token_budget", "「甲」本周已用 12 万 token。继续这次回复吗？", "2026-11-06T00:00:03Z"),
            _action("x_shell", "computer_shell", "「甲」想使用沙盒电脑。", "2026-11-06T00:00:04Z"),
            _action("x_step", "workflow_step", "工作流要开始「写稿」，现在开始吗？", "2026-11-06T00:00:05Z"),
            _action("x_sched", "schedule_create", "要建立定时任务「早报」吗？", "2026-11-06T00:00:06Z"),
            _action("x_other", "connector_write", "要调用 GitHub 的「创建 issue」吗？", "2026-11-06T00:00:07Z"),
        ],
        questions=[],
        confirm_timeout_seconds=600,
    )
    by_id = {row["id"]: row["if_unanswered"] for row in rows}
    assert by_id == {
        "x_del": "10 分钟内不答就保留这位成员。",
        "x_group": "10 分钟内不答就保留这个群组。",
        "x_budget": "10 分钟内不答，这次回复会停下。",
        "x_shell": "10 分钟内不答，这次就不用沙盒电脑。",
        "x_step": "10 分钟内不答，这一步会暂停，不会替你跳过。",
        "x_sched": "10 分钟内不答，这次改动不会保存。",
        "x_other": "10 分钟内不答，这次就不执行。",
    }
    assert rows[0]["id"] == "x_del"
    assert rows[0]["kind"] == "action"
    assert rows[0]["who"] == "主管"
    assert rows[0]["prompt"] == "主管想删除成员「甲」。"


def test_a_short_timeout_is_named_in_seconds() -> None:
    rows = decision_rows(
        actions=[_action("x", "delete_agent", "删吗？", "2026-11-06T00:00:00Z")],
        questions=[],
        confirm_timeout_seconds=90,
    )
    assert rows[0]["if_unanswered"] == "90 秒内不答就保留这位成员。"


def test_a_workflow_question_waits_without_a_timeout() -> None:
    rows = decision_rows(
        actions=[],
        questions=[
            {
                "id": "wr_1",
                "conversation_id": "c_wf",
                "message_id": "m_q",
                "who": "甲",
                "where": "工作流 · 早报",
                "prompt": "预算要不要加？",
                "updated_at": "2026-11-06T00:00:08Z",
            }
        ],
        confirm_timeout_seconds=600,
    )
    assert rows == [
        {
            "id": "wr_1",
            "kind": "workflow",
            "conversation_id": "c_wf",
            "message_id": "m_q",
            "who": "甲",
            "where": "工作流 · 早报",
            "prompt": "预算要不要加？",
            "if_unanswered": "一直等到你回答，不会自己继续。",
        }
    ]


def test_the_store_lists_a_waiting_card_and_a_question_but_not_a_running_tool(tmp_path) -> None:
    store = Store(tmp_path / "cubes.sqlite3")
    store.initialize()
    supervisor = store.supervisor()
    direct = store.direct_conversation()
    assert supervisor is not None and direct is not None
    run, _ = store.begin_run(direct["id"], "把甲删掉")
    store.create_action(
        run_id=run["id"],
        conversation_id=direct["id"],
        tool_call_id="call_wait",
        tool_name="delete_agent",
        arguments="{}",
        summary="删除成员「甲」",
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        status="awaiting",
        confirmation={"prompt": "主管想删除成员「甲」。", "confirm_label": "删除", "deny_label": "保留"},
    )
    store.create_action(
        run_id=run["id"],
        conversation_id=direct["id"],
        tool_call_id="call_run",
        tool_name="web_search",
        arguments="{}",
        summary="正在搜索",
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        status="running",
    )
    nodes, edges = rules.normalize_graph(
        [{"id": "ask", "title": "问预算", "instruction": "预算要不要加？", "kind": "ask_user"}]
    )
    workflow = store.create_workflow(
        name="早报",
        description="",
        owner_agent_id=supervisor["id"],
        created_by=supervisor["id"],
        nodes=nodes,
        edges=edges,
    )
    page = store.create_conversation(
        kind="workflow", title="工作流 · 早报", purpose="", member_ids=[supervisor["id"]]
    )
    walk = store.begin_workflow_run(workflow, conversation_id=page["id"], input="", started_by="user")
    store.update_workflow_run(walk["id"], status=rules.STATUS_WAITING)
    question = store.append_message(
        conversation_id=page["id"],
        author_id=supervisor["id"],
        author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"],
        kind="agent",
        content="预算要不要加？",
        status="complete",
    )
    store.update_workflow_step(walk["id"], "ask", status=rules.STEP_WAITING, message_id=question["id"])

    rows = collect_decisions(store, confirm_timeout_seconds=600)
    by_kind = {row["kind"]: row for row in rows}
    assert set(by_kind) == {"action", "workflow"}
    assert by_kind["action"]["prompt"] == "主管想删除成员「甲」。"
    assert by_kind["action"]["if_unanswered"] == "10 分钟内不答就保留这位成员。"
    assert by_kind["workflow"]["where"] == "工作流 · 早报"
    assert by_kind["workflow"]["prompt"] == "预算要不要加？"
    assert by_kind["workflow"]["message_id"] == question["id"]
    assert by_kind["workflow"]["if_unanswered"] == "一直等到你回答，不会自己继续。"


def _action(action_id: str, tool_name: str, prompt: str, created_at: str) -> dict:
    return {
        "id": action_id,
        "conversation_id": "c_direct",
        "tool_name": tool_name,
        "summary": prompt,
        "confirmation": {"prompt": prompt, "confirm_label": "好", "deny_label": "不"},
        "message_id": f"m_{action_id}",
        "author_name": "主管",
        "conversation_title": "主管",
        "created_at": created_at,
    }
