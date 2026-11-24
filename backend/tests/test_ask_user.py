"""ask_user: the run parks on the questions, the composer's answers resume it, nothing times out."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from app.tools.ask import AskUserArgs
from fake_upstream import call, tool_calls
from helpers import (
    action_messages,
    agent_messages,
    client_for,
    get_conversation,
    get_run,
    get_workspace,
    start_run,
    tool_results,
    wait_for_run,
)


async def wait_for_question(client, conversation_id: str, *, timeout: float = 15.0) -> dict:
    """The action row a bot parked on its questions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = await get_conversation(client, conversation_id)
        for message in action_messages(detail):
            action = message["action"]
            if action["status"] == "awaiting" and (action.get("confirmation") or {}).get("kind") == "question":
                return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no question appeared in {conversation_id}")


QUESTION = {
    "questions": [
        {
            "header": "方案",
            "question": "这次先做哪一块？",
            "options": [
                {"label": "后端", "description": "先把接口做出来"},
                {"label": "前端", "description": "先把界面做出来"},
            ],
            "recommended": "后端",
        },
        {
            "header": "时间",
            "question": "什么时候要？",
            "options": [
                {"label": "上午", "description": "中午前交"},
                {"label": "下午", "description": "下班前交"},
            ],
            "multi": True,
        },
    ]
}


def test_ask_user_parks_on_two_questions_and_resumes_with_both_answers(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "ask_user", QUESTION)),
        {"content": "好，先做后端，上午给你。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "帮我安排一下")
            waiting = await wait_for_question(client, direct_id)

            action = waiting["action"]
            confirmation = action["confirmation"]
            assert action["tool_name"] == "ask_user"
            assert [item["header"] for item in confirmation["questions"]] == ["方案", "时间"]
            assert confirmation["questions"][0]["recommended"] == "后端"
            assert confirmation["questions"][1]["multi"] is True
            assert waiting["content"] == "这次先做哪一块？；什么时候要？"

            parked = await get_run(client, run["id"])
            assert parked["status"] == "running"
            assert parked["current_step"] == "等待你回答 2 个问题"

            # The sidebar shows the first question, with no deadline.
            workspace = await get_workspace(client)
            listed = [row for row in workspace["decisions"] if row["kind"] == "question"]
            assert [row["prompt"] for row in listed] == ["这次先做哪一块？"]
            assert listed[0]["if_unanswered"] == "一直等到你回答，不会自己继续。"

            answered = await client.post(
                f"/actions/{action['id']}/answer",
                json={
                    "answers": [
                        {"question_index": 0, "labels": ["后端"], "other": None},
                        {"question_index": 1, "labels": ["上午"], "other": "尽量今天给我"},
                    ],
                    "skip": False,
                },
            )
            assert answered.status_code == 200, answered.text
            decided = answered.json()
            assert decided["status"] == "completed"
            assert decided["summary"] == "方案：后端；时间：上午、「尽量今天给我」"
            assert decided["confirmation"] is None

            again = await client.post(
                f"/actions/{action['id']}/answer",
                json={"answers": [{"question_index": 0, "labels": ["前端"]}]},
            )
            assert again.status_code == 409, again.text

            finished = await wait_for_run(client, run["id"])
            assert finished["status"] == "completed", finished

            result = dict(tool_results(upstream.requests[1]))["call_1"]
            assert result["ok"] is True
            assert result["message"] == (
                "用户的回答：\n方案：后端\n时间：上午、「尽量今天给我」"
            )
            assert result["answers"][1]["other"] == "尽量今天给我"

            replies = agent_messages(await get_conversation(client, direct_id))
            assert replies[-1]["content"] == "好，先做后端，上午给你。"

    asyncio.run(body())


def test_skipping_lets_the_bot_carry_on(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "ask_user", {"questions": [
            {
                "header": "方案",
                "question": "要几个方案？",
                "options": [{"label": "两个"}, {"label": "三个"}],
            }
        ]})),
        {"content": "那我按三个方案来。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            run = await start_run(client, direct_id, "先想几个方向")
            waiting = await wait_for_question(client, direct_id)
            skipped = await client.post(
                f"/actions/{waiting['action']['id']}/answer", json={"skip": True}
            )
            assert skipped.status_code == 200, skipped.text
            assert skipped.json()["summary"] == "用户跳过了这些问题"
            assert (await wait_for_run(client, run["id"]))["status"] == "completed"
            result = dict(tool_results(upstream.requests[1]))["call_1"]
            assert result["skipped"] is True
            assert result["message"].splitlines()[0] == "方案：用户跳过了这个问题"

    asyncio.run(body())


def test_a_scheduled_run_is_refused_and_decides_for_itself(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(call("call_1", "ask_user", {"questions": [
            {
                "header": "模板",
                "question": "用哪个模板？",
                "options": [{"label": "日报"}, {"label": "周报"}],
            }
        ]})),
        {"content": "那就用日报模板。"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            direct_id = (await get_workspace(client))["direct_conversation_id"]
            created = await client.post(
                "/schedules",
                json={
                    "title": "每天写日报",
                    "prompt": "写今天的日报",
                    "conversation_id": direct_id,
                    "kind": "interval",
                    "every_minutes": 5,
                },
            )
            assert created.status_code == 201, created.text
            schedule = created.json()

            started = await client.post(f"/schedules/{schedule['id']}/run")
            assert started.status_code == 202, started.text
            run = await wait_for_run(client, started.json()["last_run_id"])
            assert run["status"] == "completed", run

            refused = dict(tool_results(upstream.requests[1]))["call_1"]
            assert refused["ok"] is False
            assert "没有人能回答" in refused["error"]

    asyncio.run(body())


def _question(**overrides) -> dict:
    base = {
        "header": "方案",
        "question": "这次先做哪一块？",
        "options": [{"label": "后端"}, {"label": "前端"}],
    }
    base.update(overrides)
    return base


def test_a_recommendation_must_be_one_of_the_options() -> None:
    with pytest.raises(ValidationError) as caught:
        AskUserArgs.model_validate({"questions": [_question(recommended="文档")]})
    assert "推荐的选项必须是这个问题选项里的一个" in str(caught.value)


def test_duplicate_labels_and_too_many_questions_are_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        AskUserArgs.model_validate(
            {"questions": [_question(options=[{"label": "后端"}, {"label": "后端"}])]}
        )
    assert "选项名字不能重复" in str(caught.value)

    with pytest.raises(ValidationError) as caught:
        AskUserArgs.model_validate({"questions": [_question() for _ in range(5)]})
    assert "1–4 个问题" in str(caught.value)


def test_each_question_needs_two_to_four_options_and_no_own_other() -> None:
    with pytest.raises(ValidationError) as caught:
        AskUserArgs.model_validate({"questions": [_question(options=[{"label": "后端"}])]})
    assert "2–4 个选项" in str(caught.value)

    with pytest.raises(ValidationError) as caught:
        AskUserArgs.model_validate(
            {"questions": [_question(options=[{"label": "后端"}, {"label": "其他"}])]}
        )
    assert "「其他」由系统自动提供" in str(caught.value)
