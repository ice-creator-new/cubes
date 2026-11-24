"""Workflows: the graph rules, the HTTP API, and the walk the engine drives.

The walk is exercised end to end against the isolated upstream: a fan-out and its join, a
branch that leaves one arm unvisited, a question that parks the run until the user answers, a
confirmation gate, a pause that is resumed, and a restart that keeps a parked question alive.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import workflows as rules
from app.errors import ToolError
from app.store.db import SCHEMA_VERSION
from fake_upstream import call, tool_calls
from helpers import (
    client_for,
    event_names,
    get_workspace,
    store_of,
    system_prompt,
    tool_results,
)


def _nodes(*instructions: str) -> list[dict]:
    return [{"title": text, "instruction": text} for text in instructions]


def _chain(*instructions: str) -> dict:
    """A straight plan: nodes plus the edges that chain them."""
    nodes = _nodes(*instructions)
    for position, node in enumerate(nodes, start=1):
        node["id"] = f"n{position}"
    edges = [{"from": f"n{i}", "to": f"n{i + 1}"} for i in range(1, len(nodes))]
    return {"steps": nodes, "edges": edges}


async def _create(client: httpx.AsyncClient, **body) -> dict:
    payload = {"name": "早报", "description": "每天整理"}
    if "steps" not in body:
        payload.update(_chain("第一步", "第二步"))
    payload.update(body)
    response = await client.post("/workflows", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _run(client: httpx.AsyncClient, workflow_id: str, content: str = "") -> dict:
    response = await client.post(f"/workflows/{workflow_id}/run", json={"input": content})
    assert response.status_code == 202, response.text
    return response.json()


async def _wait(
    client: httpx.AsyncClient, workflow_run_id: str, *, status: str, timeout: float = 25.0
) -> dict:
    """Poll one walk until it reaches ``status``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/workflow-runs/{workflow_run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] == status:
            return payload
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"still {payload['status']} waiting for {status}: {payload}")
        await asyncio.sleep(0.05)


def _by_title(walk: dict, title: str) -> dict:
    return next(step for step in walk["steps"] if step["title"] == title)


# ---------------------------------------------------------------------- rules


def test_a_list_of_steps_becomes_a_chain() -> None:
    nodes, edges = rules.normalize_graph(
        ["先把资料找齐", {"title": "写摘要", "instruction": "写成三段", "on_fail": "skip"}]
    )
    assert [node["id"] for node in nodes] == ["n1", "n2"]
    assert nodes[0]["title"] == "先把资料找齐"
    assert nodes[0]["kind"] == rules.DO
    assert nodes[1]["on_fail"] == rules.FAIL_SKIP
    assert edges == [{"from": "n1", "to": "n2", "when": None}]


def test_a_graph_keeps_its_ids_and_branch_choices() -> None:
    nodes, edges = rules.normalize_graph(
        [
            {"id": "collect", "title": "收集", "instruction": "收集"},
            {"id": "yes", "title": "是", "instruction": "处理"},
            {"id": "no", "title": "否", "instruction": "先放着"},
            {"id": "ask", "title": "要处理吗", "instruction": "要处理吗", "kind": "branch",
             "choices": ["是", "否"]},
        ],
        [
            {"from": "collect", "to": "ask"},
            {"from": "ask", "to": "yes", "when": "是"},
            {"from": "ask", "to": "no", "when": "否"},
        ],
    )
    assert [node["id"] for node in nodes] == ["collect", "yes", "no", "ask"]
    assert nodes[3]["choices"] == ["是", "否"]
    assert len(edges) == 3
    assert rules.topological(nodes, edges) == ["collect", "ask", "yes", "no"]


def test_bad_graphs_are_refused_with_a_reason() -> None:
    with pytest.raises(rules.WorkflowInvalid, match="成环"):
        rules.normalize_graph(
            [{"id": "a", "instruction": "x"}, {"id": "b", "instruction": "y"}, {"id": "c", "instruction": "z"}],
            [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"}],
        )
    with pytest.raises(rules.WorkflowInvalid, match="没有任何连线"):
        rules.normalize_graph(
            [{"id": "a", "instruction": "x"}, {"id": "b", "instruction": "y"}, {"id": "c", "instruction": "z"}],
            [{"from": "a", "to": "b"}],
        )
    with pytest.raises(rules.WorkflowInvalid, match="不存在的节点"):
        rules.normalize_graph([{"id": "a", "instruction": "x"}], [{"from": "a", "to": "ghost"}])
    with pytest.raises(rules.WorkflowInvalid, match="只有分支节点"):
        rules.normalize_graph(
            [{"id": "a", "instruction": "x"}, {"id": "b", "instruction": "y"}],
            [{"from": "a", "to": "b", "when": "是"}],
        )
    with pytest.raises(rules.WorkflowInvalid, match="没有「是」这个选项"):
        rules.normalize_graph(
            [
                {"id": "a", "instruction": "x", "kind": "branch", "choices": ["左", "右"]},
                {"id": "b", "instruction": "y"},
            ],
            [{"from": "a", "to": "b", "when": "是"}],
        )
    with pytest.raises(rules.WorkflowInvalid, match="还没有连出去"):
        rules.normalize_graph(
            [
                {"id": "a", "instruction": "x", "kind": "branch", "choices": ["左", "右"]},
                {"id": "b", "instruction": "y"},
            ],
            [{"from": "b", "to": "a"}],
        )
    with pytest.raises(rules.WorkflowInvalid, match="不能连到自己"):
        rules.normalize_graph([{"id": "a", "instruction": "x"}], [{"from": "a", "to": "a"}])


def test_the_reachable_edges_follow_the_choice() -> None:
    nodes, edges = rules.normalize_graph(
        [
            {"id": "ask", "instruction": "要不要", "kind": "branch", "choices": ["是", "否"]},
            {"id": "yes", "instruction": "做"},
            {"id": "no", "instruction": "不做"},
        ],
        [
            {"from": "ask", "to": "yes", "when": "是"},
            {"from": "ask", "to": "no", "when": "否"},
        ],
    )
    states = {"ask": {"status": rules.STEP_DONE, "choice": "是"}}
    taken = rules.reachable_edges(nodes[0], edges, states)
    assert [edge["to"] for edge in taken] == ["yes"]
    assert rules.parse_choice("我选：是。", ["是", "否"]) == "是"
    assert rules.parse_choice("说不好", ["是", "否"]) is None
    # One choice containing another: the answer means the longer one it actually says.
    assert rules.parse_choice("不合格", ["合格", "不合格"]) == "不合格"
    assert rules.parse_choice("「不合格」，要改", ["合格", "不合格"]) == "不合格"
    assert rules.parse_choice("结论：合格", ["合格", "不合格"]) == "合格"


def test_a_loop_edge_needs_a_bound_and_must_really_go_back() -> None:
    """A back edge with ``max_loops`` is a loop; without it the same edge is an unbounded cycle."""
    steps = [
        {"id": "draft", "instruction": "写初稿"},
        {"id": "review", "instruction": "合格吗", "kind": "branch", "choices": ["合格", "不合格"]},
        {"id": "send", "instruction": "发出去"},
    ]
    forward = [
        {"from": "draft", "to": "review"},
        {"from": "review", "to": "send", "when": "合格"},
    ]
    nodes, edges = rules.normalize_graph(
        steps, [*forward, {"from": "review", "to": "draft", "when": "不合格", "max_loops": 3}]
    )
    loop = edges[-1]
    assert rules.is_loop(loop) and loop["max_loops"] == 3
    # A round resets everything forward of the target: the draft, its review, and the send.
    assert rules.loop_body(nodes, edges, loop) == ["draft", "review", "send"]
    # Forward edges stay a DAG, so the order of one round is still defined.
    assert rules.topological(nodes, rules.forward_edges(edges)) == ["draft", "review", "send"]

    with pytest.raises(rules.WorkflowInvalid, match="max_loops"):
        rules.normalize_graph(steps, [*forward, {"from": "review", "to": "draft", "when": "不合格"}])
    with pytest.raises(rules.WorkflowInvalid, match="没有绕回"):
        rules.normalize_graph(
            steps,
            [
                {"from": "draft", "to": "review"},
                {"from": "review", "to": "send", "when": "合格", "max_loops": 2},
                {"from": "review", "to": "draft", "when": "不合格", "max_loops": 2},
            ],
        )
    for bad in (0, rules.MAX_LOOPS + 1, 1.5, True):
        with pytest.raises(rules.WorkflowInvalid, match="max_loops|循环"):
            rules.normalize_graph(
                steps, [*forward, {"from": "review", "to": "draft", "when": "不合格", "max_loops": bad}]
            )


def test_a_spent_loop_closes_its_choice() -> None:
    nodes, edges = rules.normalize_graph(
        [
            {"id": "draft", "instruction": "写"},
            {"id": "review", "instruction": "合格吗", "kind": "branch", "choices": ["合格", "不合格"]},
            {"id": "send", "instruction": "发"},
        ],
        [
            {"from": "draft", "to": "review"},
            {"from": "review", "to": "send", "when": "合格"},
            {"from": "review", "to": "draft", "when": "不合格", "max_loops": 2},
        ],
    )
    review = nodes[1]
    key = rules.edge_key(edges[-1])
    assert rules.available_choices(review, edges, {}) == (["合格", "不合格"], [])
    assert rules.available_choices(review, edges, {key: 1}) == (["合格", "不合格"], [])
    assert rules.available_choices(review, edges, {key: 2}) == (["合格"], ["不合格"])
    # A spent loop edge is dead even when the branch somehow picked it.
    states = {"review": {"status": rules.STEP_DONE, "choice": "不合格"}}
    assert rules.reachable_edges(review, edges, states, {key: 2}) == []
    assert rules.reachable_edges(review, edges, states, {key: 1}) == [edges[-1]]


def test_markdown_shows_the_edges() -> None:
    nodes, edges = rules.normalize_graph(
        [
            {"id": "collect", "instruction": "收集"},
            {"id": "ask", "instruction": "要发吗", "kind": "branch", "choices": ["发", "不发"]},
            {"id": "send", "instruction": "发出"},
            {"id": "hold", "instruction": "存档"},
        ],
        [
            {"from": "collect", "to": "ask"},
            {"from": "ask", "to": "send", "when": "发"},
            {"from": "ask", "to": "hold", "when": "不发"},
        ],
    )
    markdown = rules.render_markdown(
        {"name": "早报", "description": "", "nodes": nodes, "edges": edges}
    )
    assert markdown.startswith("---\nname: 早报")
    assert "2. [分支] 要发吗" in markdown
    assert "→ 当「发」接着 发出" in markdown
    looped = rules.render_markdown(
        {
            "name": "改稿",
            "description": "",
            "nodes": nodes,
            "edges": [*edges, {"from": "ask", "to": "collect", "when": "不发", "max_loops": 2}],
        }
    )
    assert "↺ 当「不发」回到 collect（最多循环 2 轮）" in looped.replace("收集", "collect")


# ---------------------------------------------------------------------- HTTP


def test_workflow_crud_and_versioning(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(client)
            assert workflow["owner_name"] == "主管"
            assert workflow["version"] == 1
            assert [node["id"] for node in workflow["steps"]] == ["n1", "n2"]
            assert workflow["edges"] == [{"from": "n1", "to": "n2", "when": None, "max_loops": None}]

            listed = (await client.get("/workflows")).json()["workflows"]
            assert [item["name"] for item in listed] == ["早报"]

            duplicate = await client.post(
                "/workflows", json={"name": "早报", "steps": _nodes("换个说法")}
            )
            assert duplicate.status_code == 409

            patched = await client.patch(
                f"/workflows/{workflow['id']}", json=_chain("只剩一步")
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["version"] == 2
            assert len(patched.json()["steps"]) == 1
            assert patched.json()["edges"] == []

            detail = (await client.get(f"/workflows/{workflow['id']}")).json()
            assert "1. [执行] 只剩一步" in detail["markdown"]

            cycle = await client.post(
                "/workflows",
                json={
                    "name": "环",
                    "steps": [
                        {"id": "a", "instruction": "一"},
                        {"id": "b", "instruction": "二"},
                    ],
                    "edges": [
                        {"from": "a", "to": "b"},
                        {"from": "b", "to": "a"},
                    ],
                },
            )
            assert cycle.status_code == 422
            assert "成环" in cycle.json()["detail"]

            removed = await client.delete(f"/workflows/{workflow['id']}")
            assert removed.status_code == 204
            assert (await client.get("/workflows")).json()["workflows"] == []

    asyncio.run(body())


# ---------------------------------------------------------------------- the walk


def test_a_fan_out_and_its_join(make_backend, upstream) -> None:
    """One node feeds two branches; the join runs once, after both have settled."""
    upstream.script = [
        {"content": "入口做完"},
        {"content": "左支路做完"},
        {"content": "右支路做完"},
        {"content": "汇合做完"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "start", "title": "入口", "instruction": "入口"},
                    {"id": "left", "title": "左", "instruction": "左支路"},
                    {"id": "right", "title": "右", "instruction": "右支路"},
                    {"id": "join", "title": "汇合", "instruction": "汇合"},
                ],
                edges=[
                    {"from": "start", "to": "left"},
                    {"from": "start", "to": "right"},
                    {"from": "left", "to": "join"},
                    {"from": "right", "to": "join"},
                ],
            )
            started = await _run(client, workflow["id"])
            done = await _wait(client, started["workflow_run"]["id"], status="done")

            assert done["cursor"] == 3
            assert {step["title"]: step["status"] for step in done["steps"]} == {
                "入口": "done",
                "左": "done",
                "右": "done",
                "汇合": "done",
            }
            assert _by_title(done, "汇合")["output"] == "汇合做完"
            # The worker is single: plan order decided, and each node ran exactly once.
            assert len(upstream.requests) == 4
            assert "第 2/4 个节点" not in _last_user(upstream.requests[1])

    asyncio.run(body())


def test_a_loop_runs_its_body_again_until_the_branch_moves_on(make_backend, upstream) -> None:
    """A review rejects the draft twice; each rejection rewinds the draft and the review."""
    verdicts = iter(["不合格", "不合格", "合格"])

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        if "合格吗" in asked:
            return {"content": next(verdicts)}
        if "写稿" in asked:
            return {"content": "一稿"}
        return {"content": "已发出"}

    upstream.script = [script]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "draft", "title": "写稿", "instruction": "写稿"},
                    {"id": "review", "title": "审稿", "instruction": "合格吗", "kind": "branch",
                     "choices": ["合格", "不合格"]},
                    {"id": "send", "title": "发出", "instruction": "发出去"},
                ],
                edges=[
                    {"from": "draft", "to": "review"},
                    {"from": "review", "to": "send", "when": "合格"},
                    {"from": "review", "to": "draft", "when": "不合格", "max_loops": 3},
                ],
            )
            assert workflow["edges"][-1]["max_loops"] == 3
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="done")

            draft, review, send = (_by_title(walk, title) for title in ("写稿", "审稿", "发出"))
            assert walk["loops"] == {"review->draft#不合格": 2}
            assert (draft["round"], review["round"], send["round"]) == (2, 2, 0)
            assert [item["round"] for item in draft["history"]] == [0, 1]
            assert [item["choice"] for item in review["history"]] == ["不合格", "不合格"]
            assert review["choice"] == "合格" and send["status"] == "done"
            # Two rejected rounds and the accepted one: 3 drafts + 3 reviews + the send.
            assert len(upstream.requests) == 7
            # A rewound node is told which round it is on.
            third_draft = [r for r in upstream.requests if "写稿" in _last_user(r)][-1]
            assert "第 3 轮" in _last_user(third_draft)

    asyncio.run(body())


def test_a_spent_loop_is_not_offered_and_the_walk_leaves(make_backend, upstream) -> None:
    """The review keeps rejecting; after two loops it may only pass, and the walk finishes."""

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        if "合格吗" in asked:
            return {"content": "合格" if "已经循环到上限" in asked else "不合格"}
        if "写稿" in asked:
            return {"content": "一稿"}
        return {"content": "已发出"}

    upstream.script = [script]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "draft", "title": "写稿", "instruction": "写稿"},
                    {"id": "review", "title": "审稿", "instruction": "合格吗", "kind": "branch",
                     "choices": ["合格", "不合格"]},
                    {"id": "send", "title": "发出", "instruction": "发出去"},
                ],
                edges=[
                    {"from": "draft", "to": "review"},
                    {"from": "review", "to": "send", "when": "合格"},
                    {"from": "review", "to": "draft", "when": "不合格", "max_loops": 2},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="done")

            assert walk["loops"] == {"review->draft#不合格": 2}
            assert _by_title(walk, "审稿")["round"] == 2
            assert _by_title(walk, "审稿")["choice"] == "合格"
            assert _by_title(walk, "发出")["status"] == "done"
            last_review = [r for r in upstream.requests if "合格吗" in _last_user(r)][-1]
            assert "「不合格」已经循环到上限" in _last_user(last_review)
            assert "「合格」。只输出" in _last_user(last_review)

    asyncio.run(body())


def test_a_spent_loop_answer_does_not_take_another_round(make_backend, upstream) -> None:
    """Naming the closed choice anyway is no choice: the branch fails instead of looping."""

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        return {"content": "不合格" if "合格吗" in asked else "一稿"}

    upstream.script = [script]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "draft", "title": "写稿", "instruction": "写稿"},
                    {"id": "review", "title": "审稿", "instruction": "合格吗", "kind": "branch",
                     "choices": ["合格", "不合格"]},
                    {"id": "send", "title": "发出", "instruction": "发出去"},
                ],
                edges=[
                    {"from": "draft", "to": "review"},
                    {"from": "review", "to": "send", "when": "合格"},
                    {"from": "review", "to": "draft", "when": "不合格", "max_loops": 1},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="failed")

            assert walk["loops"] == {"review->draft#不合格": 1}
            assert "没有给出可用的选项" in walk["summary"]
            # 2 drafts + 2 reviews: the loop was not taken a second time.
            assert len(upstream.requests) == 4

    asyncio.run(body())


def test_a_plain_loop_edge_repeats_a_fixed_number_of_rounds(make_backend, upstream) -> None:
    """A loop edge out of a plain node is taken every time it settles, until it is spent."""
    upstream.script = [lambda requests, index: {"content": f"做完 {index}"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "a", "title": "收集", "instruction": "收集"},
                    {"id": "b", "title": "整理", "instruction": "整理"},
                    {"id": "c", "title": "汇报", "instruction": "汇报"},
                ],
                edges=[
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "a", "max_loops": 2},
                    {"from": "b", "to": "c"},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="done")

            assert walk["loops"] == {"b->a#": 2}
            assert [_by_title(walk, title)["round"] for title in ("收集", "整理", "汇报")] == [2, 2, 0]
            # c waits until the loop is spent, then runs once: 3 × (a, b) + c.
            assert len(upstream.requests) == 7

    asyncio.run(body())


def test_a_new_round_reopens_an_arm_an_earlier_round_skipped(make_backend, upstream) -> None:
    """Round one's branch skipped the side arm; round two picks it, so it runs."""
    picks = iter(["改", "停"])

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        if "怎么办" in asked:
            return {"content": next(picks)}
        return {"content": "好了"}

    upstream.script = [script]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "work", "title": "干活", "instruction": "干活"},
                    {"id": "pick", "title": "判断", "instruction": "怎么办", "kind": "branch",
                     "choices": ["改", "停"]},
                    {"id": "fix", "title": "修改", "instruction": "修改"},
                    {"id": "stop", "title": "收尾", "instruction": "收尾"},
                ],
                edges=[
                    {"from": "work", "to": "pick"},
                    {"from": "pick", "to": "fix", "when": "改"},
                    {"from": "pick", "to": "stop", "when": "停"},
                    {"from": "fix", "to": "work", "max_loops": 3},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="done")

            assert walk["loops"] == {"fix->work#": 1}
            assert _by_title(walk, "收尾")["status"] == "done"
            # Round two picked 停: 修改 was not reached in it, so it is skipped again.
            assert _by_title(walk, "修改")["status"] == "skipped"
            assert [item["status"] for item in _by_title(walk, "修改")["history"]] == ["done"]

    asyncio.run(body())


def test_a_branch_runs_one_arm_and_skips_the_other(make_backend, upstream) -> None:
    def script(requests, index):
        asked = _last_user(requests[index - 1])
        if "要大写吗" in asked:
            return {"content": "是"}
        if "大写" in asked:
            return {"content": "已大写"}
        if "原样" in asked:
            return {"content": "原样保存"}
        return {"content": "汇总完成"}

    upstream.script = [script]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "ask", "title": "要改吗", "instruction": "要大写吗", "kind": "branch",
                     "choices": ["是", "否"]},
                    {"id": "yes", "title": "大写", "instruction": "转成大写"},
                    {"id": "no", "title": "原样", "instruction": "原样保留"},
                    {"id": "end", "title": "汇总", "instruction": "汇总一下"},
                ],
                edges=[
                    {"from": "ask", "to": "yes", "when": "是"},
                    {"from": "ask", "to": "no", "when": "否"},
                    {"from": "yes", "to": "end"},
                    {"from": "no", "to": "end"},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="done")

            assert _by_title(walk, "要改吗")["choice"] == "是"
            assert _by_title(walk, "大写")["status"] == "done"
            assert _by_title(walk, "原样")["status"] == "skipped"
            assert _by_title(walk, "汇总")["status"] == "done"
            # The unvisited arm never reached the model.
            assert not any("原样保留" in _last_user(request) for request in upstream.requests)

    asyncio.run(body())


def test_a_question_parks_the_walk_until_the_user_answers(make_backend, upstream) -> None:
    upstream.script = [{"content": "资料齐了"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "a", "title": "收集", "instruction": "收集资料"},
                    {"id": "b", "title": "确认时间", "instruction": "要哪几天的数据？",
                     "kind": "ask_user"},
                ],
                edges=[{"from": "a", "to": "b"}],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]

            waiting = await _wait(client, workflow_run_id, status="waiting_user")
            assert _by_title(waiting, "确认时间")["status"] == rules.STEP_WAITING

            conversation = (
                await client.get(f"/conversations/{started['conversation_id']}")
            ).json()
            question = [
                message
                for message in conversation["messages"]
                if message["content"] == "要哪几天的数据？"
            ]
            assert len(question) == 1 and question[0]["kind"] == "agent"

            answered = await client.post(
                f"/workflow-runs/{workflow_run_id}/answer", json={"input": "最近三天"}
            )
            assert answered.status_code == 200, answered.text

            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "确认时间")["output"] == "最近三天"
            assert len(upstream.requests) == 1  # the question cost no model call

    asyncio.run(body())


def test_a_confirm_gate_waits_for_the_card(make_backend, upstream) -> None:
    upstream.script = [{"content": "做完了"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[{"id": "p", "title": "发布", "instruction": "发出去", "gate": "confirm"}],
                edges=[],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            conversation_id = started["conversation_id"]
            await _wait(client, workflow_run_id, status="running")
            card = await _wait_for_card(client, conversation_id)
            assert "发布" in card["content"]
            assert card["action"]["confirmation"]["confirm_label"] == "开始"

            approved = await client.post(
                f"/actions/{card['action']['id']}/confirm", json={"approve": True}
            )
            assert approved.status_code == 200, approved.text

            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "发布")["output"] == "做完了"

    asyncio.run(body())


def test_pause_parks_the_walk_and_resume_continues_it(make_backend, upstream) -> None:
    upstream.script = [{"content": "第一段"}, {"content": "第二段"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "a", "title": "第一步", "instruction": "第一步"},
                    {"id": "b", "title": "闸门", "instruction": "第二步", "gate": "confirm"},
                ],
                edges=[{"from": "a", "to": "b"}],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            conversation_id = started["conversation_id"]
            await _wait(client, workflow_run_id, status="running")
            card = await _wait_for_card(client, conversation_id)

            paused = await client.post(f"/workflow-runs/{workflow_run_id}/pause")
            assert paused.status_code == 200, paused.text
            await client.post(
                f"/actions/{card['action']['id']}/confirm", json={"approve": True}
            )
            parked = await _wait(client, workflow_run_id, status="paused")
            assert _by_title(parked, "闸门")["status"] == "pending"

            resumed = await client.post(f"/workflow-runs/{workflow_run_id}/resume")
            assert resumed.status_code == 200, resumed.text
            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "闸门")["output"] == "第二段"

    asyncio.run(body())


def test_skip_moves_past_the_node_that_is_waiting(make_backend, upstream) -> None:
    upstream.script = [{"content": "第一段"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "a", "title": "第一步", "instruction": "第一步"},
                    {"id": "b", "title": "闸门", "instruction": "第二步", "gate": "confirm"},
                ],
                edges=[{"from": "a", "to": "b"}],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            conversation_id = started["conversation_id"]
            await _wait(client, workflow_run_id, status="running")
            card = await _wait_for_card(client, conversation_id)
            await client.post(f"/workflow-runs/{workflow_run_id}/pause")
            await client.post(
                f"/actions/{card['action']['id']}/confirm", json={"approve": True}
            )
            await _wait(client, workflow_run_id, status="paused")

            skipped = await client.post(f"/workflow-runs/{workflow_run_id}/nodes/b/skip")
            assert skipped.status_code == 200, skipped.text
            assert _by_title(skipped.json(), "闸门")["status"] == "skipped"

            await client.post(f"/workflow-runs/{workflow_run_id}/resume")
            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "闸门")["status"] == "skipped"

    asyncio.run(body())


def test_a_disabled_workflow_does_not_start(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(client, enabled=False)
            response = await client.post(f"/workflows/{workflow['id']}/run", json={})
            assert response.status_code == 409
            assert "启用" in response.json()["detail"]

    asyncio.run(body())


# ---------------------------------------------------------------------- bot-written plans


def test_a_bot_writes_a_graph_and_sees_it_in_the_list(make_backend, upstream) -> None:
    upstream.script = [
        tool_calls(
            call(
                "call_1",
                "workflow_create",
                {
                    "name": "周报",
                    "description": "每周五整理",
                    "steps": [
                        {"id": "collect", "title": "收集", "instruction": "收集本周进展"},
                        {"id": "split", "title": "分两支", "instruction": "要不要发？",
                         "kind": "branch", "choices": ["发", "不发"]},
                        {"id": "send", "title": "发出", "instruction": "发给团队"},
                        {"id": "keep", "title": "存档", "instruction": "写进笔记"},
                    ],
                    "edges": [
                        {"from": "collect", "to": "split"},
                        {"from": "split", "to": "send", "when": "发"},
                        {"from": "split", "to": "keep", "when": "不发"},
                    ],
                },
            )
        ),
        {"content": "写好了，可以直接跑"},
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            await _start_goal(client, workspace["direct_conversation_id"], "帮我写个周报工作流")

            workflows = (await client.get("/workflows")).json()["workflows"]
            assert [item["name"] for item in workflows] == ["周报"]
            # A plan a bot wrote is usable at once; the user can still stop it in the panel.
            assert workflows[0]["enabled"] is True
            assert len(workflows[0]["steps"]) == 4
            assert len(workflows[0]["edges"]) == 3
            result = tool_results(upstream.requests[1])[0][1]
            assert result["ok"] is True
            assert result["nodes"] == 4 and result["edges"] == 3

    asyncio.run(body())


def test_a_member_cannot_touch_another_bots_plan(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            owner = store.supervisor()
            member = (
                await client.post("/agents", json={"name": "小报", "instructions": "整理消息"})
            ).json()

            nodes, edges = rules.normalize_graph([{"instruction": "做点事"}])
            workflow = store.create_workflow(
                name="主管的计划",
                description="",
                owner_agent_id=owner["id"],
                created_by=owner["id"],
                nodes=nodes,
                edges=edges,
                enabled=True,
            )

            from app.tools import workflow as workflow_tools

            handlers = {
                handler.name: handler for handler in workflow_tools.handlers(store, _no_launcher)
            }

            class Ctx:
                def __init__(self, author: dict) -> None:
                    self.author = author
                    self.store = store

                def publish(self, *_args) -> None:  # pragma: no cover - nothing to publish
                    pass

            ctx = Ctx(member)
            with pytest.raises(ToolError) as denied:
                await handlers["workflow_delete"].run(ctx, {"name": "主管的计划"})
            assert "只能改自己的" in str(denied.value)
            assert store.get_workflow(workflow["id"]) is not None

            ctx.author = owner
            deleted = await handlers["workflow_delete"].run(ctx, {"name": "主管的计划"})
            assert deleted.result["ok"] is True
            assert store.get_workflow(workflow["id"]) is None

    asyncio.run(body())


def test_the_reconnect_snapshot_carries_the_walks_page(make_backend, upstream) -> None:
    """A page reopens with its own walk, finished or not; a chat carries none."""
    upstream.script = [{"content": "做完了"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client, steps=[{"id": "only", "title": "唯一", "instruction": "做"}], edges=[]
            )
            started = await _run(client, workflow["id"])
            page_id = started["conversation_id"]
            walk_id = started["workflow_run"]["id"]
            await _wait(client, walk_id, status="done")

            events = await _collect(client, duration=0.5, conversation=page_id)
            walk = next(p for name, p in events if name == "snapshot")["workflow_run"]
            assert walk is not None
            assert walk["id"] == walk_id
            assert walk["conversation_id"] == page_id
            assert walk["status"] == "done"

            # The owner's chat is not a walk page: its snapshot carries no walk of its own.
            workspace = await get_workspace(client)
            chat_id = workspace["direct_conversation_id"]
            chat_events = await _collect(client, duration=0.5, conversation=chat_id)
            assert next(p for name, p in chat_events if name == "snapshot")["workflow_run"] is None

    asyncio.run(body())


def test_a_walk_gets_a_page_of_its_own_with_one_way_in(make_backend, upstream) -> None:
    """The steps happen on their own page; the owner's chat keeps one row that leads there."""
    upstream.script = [{"content": "第一段"}, {"content": "第二段"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            member = store.create_agent(name="甲", instructions="做研究", kind="member")
            chat = store.ensure_direct_conversation(member["id"])
            workflow = await _create(client, owner_agent_id=member["id"])
            started = await _run(client, workflow["id"])
            page_id = started["conversation_id"]
            assert page_id != chat["id"]
            page = store.get_conversation(page_id)
            assert page["kind"] == "workflow"
            assert page["title"] == "工作流 · 早报"
            assert page["member_ids"] == [member["id"]]
            await _wait(client, started["workflow_run"]["id"], status="done")

            # The steps are on the page, in order, and nobody typed anything there.
            detail = (await client.get(f"/conversations/{page_id}")).json()
            assert [m["content"] for m in detail["messages"] if m["kind"] == "agent"] == [
                "第一段",
                "第二段",
            ]
            assert [m for m in detail["messages"] if m["kind"] == "user"] == []

            # The owner's chat has exactly one row, it points at the page, and it says how it ended.
            owner_view = (await client.get(f"/conversations/{chat['id']}")).json()
            rows = [
                m
                for m in owner_view["messages"]
                if m["kind"] == "action" and m["action"]["tool_name"] == "workflow_run"
            ]
            assert len(rows) == 1
            assert rows[0]["action"]["target_conversation_id"] == page_id
            assert rows[0]["content"] == "工作流「早报」跑完了（2/2）"
            assert [m for m in owner_view["messages"] if m["kind"] == "agent"] == []

    asyncio.run(body())


def test_a_page_is_its_own_conversation_so_the_owner_can_still_be_answering(
    make_backend, upstream
) -> None:
    """A walk never waits for its owner's chat: the page has its own run slot."""
    upstream.chunk_delay = 0.15
    upstream.chunk_count = 6
    upstream.script = [{"content": "慢慢回答"}, {"content": "步骤结果"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            chat_id = workspace["direct_conversation_id"]
            workflow = await _create(
                client, steps=[{"id": "a", "title": "唯一", "instruction": "做"}], edges=[]
            )
            reply = await client.post(f"/conversations/{chat_id}/runs", json={"content": "你好"})
            assert reply.status_code == 202
            started = await _run(client, workflow["id"])
            assert started["workflow_run"]["status"] == "running"

            done = await _wait(client, started["workflow_run"]["id"], status="done")
            assert done["steps"][0]["output"] == "步骤结果"
            # Both went through: the chat's reply, and the page's step.
            chat = (await client.get(f"/conversations/{chat_id}")).json()
            assert [m["content"] for m in chat["messages"] if m["kind"] == "agent"] == ["慢慢回答"]
            page = (await client.get(f"/conversations/{started['conversation_id']}")).json()
            assert [m["content"] for m in page["messages"] if m["kind"] == "agent"] == ["步骤结果"]

    asyncio.run(body())


def test_the_same_workflow_is_not_started_twice_while_it_runs(make_backend, upstream) -> None:
    """Different plans get their own pages; one plan cannot be walking two of them."""
    upstream.script = [{"content": "x"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                name="甲流",
                steps=[{"id": "q", "title": "问", "instruction": "几点？", "kind": "ask_user"}],
                edges=[],
            )
            started = await _run(client, workflow["id"])
            walk_id = started["workflow_run"]["id"]
            await _wait(client, walk_id, status="waiting_user")
            again = await client.post(f"/workflows/{workflow['id']}/run", json={"input": ""})
            assert again.status_code == 409
            # A paused walk is continued in place instead of started a second time.
            assert (await client.post(f"/workflow-runs/{walk_id}/pause")).status_code == 200
            resumed = await client.post(f"/workflows/{workflow['id']}/run", json={"input": ""})
            assert resumed.status_code == 202, resumed.text
            assert resumed.json()["workflow_run"]["id"] == walk_id

    asyncio.run(body())


def test_answering_in_the_chat_resumes_a_parked_question(make_backend, upstream) -> None:
    """The user can answer by typing in the chat; no special panel call is needed."""
    upstream.script = [{"content": "收到，继续"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "q", "title": "问", "instruction": "几点？", "kind": "ask_user"},
                    {"id": "d", "title": "做", "instruction": "做"},
                ],
                edges=[{"from": "q", "to": "d"}],
            )
            started = await _run(client, workflow["id"])
            chat_id = started["conversation_id"]
            walk_id = started["workflow_run"]["id"]
            await _wait(client, walk_id, status="waiting_user")
            sent = await client.post(f"/conversations/{chat_id}/runs", json={"content": "三点"})
            assert sent.status_code == 202, sent.text
            done = await _wait(client, walk_id, status="done")
            assert _by_title(done, "问")["output"] == "三点"

    asyncio.run(body())


def test_a_message_that_interrupts_a_walk_parks_it_instead_of_ending_it(
    make_backend, upstream
) -> None:
    upstream.chunk_delay = 0.3
    upstream.chunk_count = 8
    upstream.script = [{"content": "很长的一步"}, {"content": "插话的回答"}, {"content": "续上了"}]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client, steps=[{"id": "a", "title": "唯一", "instruction": "做"}], edges=[]
            )
            started = await _run(client, workflow["id"])
            chat_id = started["conversation_id"]
            walk_id = started["workflow_run"]["id"]
            await _wait(client, walk_id, status="running")
            await asyncio.sleep(0.3)
            # 「停止并发送」: the default send now joins the running step instead.
            sent = await client.post(
                f"/conversations/{chat_id}/runs", json={"content": "先等等", "mode": "interrupt"}
            )
            assert sent.status_code == 202
            parked = await _wait(client, walk_id, status="paused")
            assert _by_title(parked, "唯一")["status"] == rules.STEP_PENDING
            resumed = await client.post(f"/workflow-runs/{walk_id}/resume")
            assert resumed.status_code == 200, resumed.text
            await _wait(client, walk_id, status="done")

    asyncio.run(body())


def test_a_linear_plan_from_before_graphs_migrates_into_a_chain(tmp_path) -> None:
    """v21 rewrites every stored plan: one chain of edges, and a node id on each state row."""
    import json
    import sqlite3

    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    store = Store(path)
    store.initialize()
    workflow = store.create_workflow(
        name="旧计划",
        description="",
        owner_agent_id=store.supervisor()["id"],
        created_by="user",
        nodes=[
            {"id": "n1", "title": "一", "instruction": "一", "kind": "do"},
            {"id": "n2", "title": "二", "instruction": "二", "kind": "do"},
        ],
    )
    conversation = store.ensure_direct_conversation(store.supervisor()["id"])
    run = store.begin_workflow_run(
        workflow, conversation_id=conversation["id"], input="", started_by="user"
    )
    store.close()

    # Rewind the file to how a pre-graph database looked: no edges, no node ids, version 20.
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value = '20' WHERE key = 'schema_version'")
    conn.execute("UPDATE workflows SET edges = '[]'")
    conn.execute("UPDATE workflow_runs SET edges = '[]'")
    conn.execute("UPDATE workflow_steps SET node_id = ''")
    conn.commit()
    conn.close()

    store = Store(path)
    store.initialize()
    try:
        migrated = store.get_workflow(workflow["id"])
        assert migrated["edges"] == [{"from": "n1", "to": "n2", "when": None}]
        walk = store.get_workflow_run(run["id"])
        assert walk["edges"] == [{"from": "n1", "to": "n2", "when": None}]
        assert [state["node_id"] for state in store.workflow_step_states(run["id"])] == ["n1", "n2"]
        assert json.loads(
            sqlite3.connect(str(path)).execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        ) == SCHEMA_VERSION
    finally:
        store.close()


# ---------------------------------------------------------------------- restart


def test_a_restart_pauses_a_walk_instead_of_failing_it(make_backend) -> None:
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            workflow = await _create(client)
            owner = store.supervisor()
            conversation = store.ensure_direct_conversation(owner["id"])
            run = store.begin_workflow_run(
                store.get_workflow(workflow["id"]),
                conversation_id=conversation["id"],
                input="",
                started_by="user",
            )
            store.update_workflow_run(run["id"], status=rules.STATUS_RUNNING)
            store.update_workflow_step(run["id"], "n1", status=rules.STEP_RUNNING)

            assert store.pause_live_workflow_runs() == 1
            paused = store.get_workflow_run(run["id"])
            assert paused["status"] == rules.STATUS_PAUSED
            assert store.step_state(run["id"], "n1")["status"] == rules.STEP_PENDING

            # A walk parked on a question was not mid-node: its question stays answerable.
            waiting = store.begin_workflow_run(
                store.get_workflow(workflow["id"]),
                conversation_id=conversation["id"],
                input="",
                started_by="user",
            )
            store.update_workflow_run(waiting["id"], status=rules.STATUS_WAITING)
            store.update_workflow_step(waiting["id"], "n2", status=rules.STEP_WAITING)
            assert store.pause_live_workflow_runs() == 0
            assert store.get_workflow_run(waiting["id"])["status"] == rules.STATUS_WAITING
            assert store.step_state(waiting["id"], "n2")["status"] == rules.STEP_WAITING

    asyncio.run(body())


# ---------------------------------------------------------------------- helpers


async def _no_launcher(*_args, **_kwargs):  # pragma: no cover - never reached here
    return None


def _last_user(request: dict) -> str:
    for message in reversed(request["messages"]):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


async def _start_goal(client: httpx.AsyncClient, conversation_id: str, content: str) -> dict:
    response = await client.post(
        f"/conversations/{conversation_id}/runs", json={"content": content}
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    deadline = asyncio.get_running_loop().time() + 25
    while asyncio.get_running_loop().time() < deadline:
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in ("completed", "stopped", "error", "limited"):
            return run
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


async def _wait_for_card(client: httpx.AsyncClient, conversation_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        detail = (await client.get(f"/conversations/{conversation_id}")).json()
        card = next(
            (
                message
                for message in detail["messages"]
                if message["kind"] == "action"
                and message["action"]["tool_name"] == "workflow_step"
            ),
            None,
        )
        if card is not None:
            return card
        assert asyncio.get_running_loop().time() < deadline, "no card appeared"
        await asyncio.sleep(0.05)


async def _collect(
    client: httpx.AsyncClient, *, duration: float, conversation: str | None = None
) -> list[tuple[str, dict]]:
    from helpers import collect_events

    url = "/events" if conversation is None else f"/events?conversation_id={conversation}"
    return await collect_events(client, url, duration=duration)


def test_a_walk_chat_from_before_v22_is_folded_into_its_owners_chat(tmp_path) -> None:
    """v22 moves the old per-walk conversation's rows into the owner's chat and drops it."""
    import sqlite3

    from app.store import Store

    path = tmp_path / "cubes.sqlite3"
    store = Store(path)
    store.initialize()
    supervisor = store.supervisor()
    chat = store.ensure_direct_conversation(supervisor["id"])
    store.append_message(
        conversation_id=chat["id"], author_id=None, author_name="你", avatar_seed=None,
        kind="user", content="早", status="complete",
    )
    workflow = store.create_workflow(
        name="旧流", description="", owner_agent_id=supervisor["id"], created_by="user",
        nodes=[{"id": "n1", "title": "一", "instruction": "一", "kind": "do"}],
    )
    old = store.create_conversation(
        kind="workflow", title="工作流 · 旧流", purpose="", member_ids=[supervisor["id"]]
    )
    walk = store.begin_workflow_run(
        workflow, conversation_id=old["id"], input="", started_by="user"
    )
    store.append_message(
        conversation_id=old["id"], author_id=supervisor["id"], author_name=supervisor["name"],
        avatar_seed=supervisor["avatar_seed"], kind="agent", content="步骤结果", status="complete",
    )
    store.close()

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value = '21' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    store = Store(path)
    store.initialize()
    try:
        assert store.get_conversation(old["id"]) is None
        assert store.get_workflow_run(walk["id"])["conversation_id"] == chat["id"]
        rows = store.list_messages(chat["id"])
        assert [(m["kind"], m["content"], m["seq"]) for m in rows] == [
            ("user", "早", 1),
            ("agent", "步骤结果", 2),
        ]
    finally:
        store.close()


def test_starting_a_walk_ends_the_bots_turn_instead_of_it_doing_the_work_too(
    make_backend, upstream
) -> None:
    """workflow_run hands off: the bot stops there, it does not research the plan itself."""
    backend = make_backend()
    store = store_of(backend)

    async def body() -> None:
        async with client_for(backend) as client:
            workspace = await get_workspace(client)
            chat_id = workspace["direct_conversation_id"]
            workflow = await _create(
                client,
                steps=[{"id": "q", "title": "问", "instruction": "要什么？", "kind": "ask_user"}],
                edges=[],
            )
            upstream.script = [
                tool_calls(call("c1", "workflow_run", {"name": workflow["name"]})),
                # Nothing may ask for this: the turn ended with the hand-off.
                tool_calls(call("c2", "web_search", {"query": "我替它查"})),
                {"content": "我不该出现"},
            ]
            run = await _start_goal(client, chat_id, "用流水线调研一下")
            assert run["status"] == "completed", run
            assert upstream.request_count == 1

            detail = await client.get(f"/conversations/{chat_id}")
            rows = [
                m
                for m in detail.json()["messages"]
                if m["kind"] == "action" and m["action"]["tool_name"] == "workflow_run"
            ]
            # One row, not two: the tool's own row is the way in, and it points at the page.
            assert len(rows) == 1
            page_id = rows[0]["action"]["target_conversation_id"]
            assert store.get_conversation(page_id)["kind"] == "workflow"
            assert rows[0]["content"].startswith("开始了工作流")

    asyncio.run(body())


def test_the_supervisor_can_switch_anyones_workflow_on_and_off(make_backend) -> None:
    """A member switches its own plan; the supervisor switches anyone's — off or back on."""
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            store = store_of(backend)
            owner = store.supervisor()
            member = (
                await client.post("/agents", json={"name": "小报", "instructions": "整理"})
            ).json()
            nodes, edges = rules.normalize_graph([{"instruction": "做点事"}])
            plan = store.create_workflow(
                name="成员的战报",
                description="",
                owner_agent_id=member["id"],
                created_by=member["id"],
                nodes=nodes,
                edges=edges,
                enabled=True,
            )

            from app.tools import workflow as workflow_tools

            handlers = {
                handler.name: handler for handler in workflow_tools.handlers(store, _no_launcher)
            }

            class Ctx:
                def __init__(self, author: dict) -> None:
                    self.author = author
                    self.store = store

                def publish(self, *_args) -> None:
                    pass

            ctx = Ctx(owner)
            await handlers["workflow_update"].run(ctx, {"name": "成员的战报", "enabled": False})
            assert store.get_workflow(plan["id"])["enabled"] is False
            # A stopped plan cannot start until it is switched back on.
            assert (await client.post(f"/workflows/{plan['id']}/run", json={})).status_code == 409
            await handlers["workflow_update"].run(ctx, {"name": "成员的战报", "enabled": True})
            assert store.get_workflow(plan["id"])["enabled"] is True

            # The member itself may still switch its own plan off, but not the supervisor's.
            mine = Ctx(member)
            await handlers["workflow_update"].run(mine, {"name": "成员的战报", "enabled": False})
            assert store.get_workflow(plan["id"])["enabled"] is False
            supervisors_plan = store.create_workflow(
                name="主管的计划",
                description="",
                owner_agent_id=owner["id"],
                created_by=owner["id"],
                nodes=nodes,
                edges=edges,
            )
            with pytest.raises(ToolError) as denied:
                await handlers["workflow_update"].run(
                    mine, {"name": "主管的计划", "enabled": False}
                )
            assert "只能改自己的" in str(denied.value)
            assert store.get_workflow(supervisors_plan["id"])["enabled"] is True

    asyncio.run(body())


# ---------------------------------------------------------------------- parallel branches


async def _sample(
    client: httpx.AsyncClient, workflow_run_id: str, *, status: str = "done", timeout: float = 25.0
) -> list[dict]:
    """Poll one walk every 20 ms and keep every payload, so a test can read what overlapped."""
    seen: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/workflow-runs/{workflow_run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        seen.append(payload)
        if payload["status"] == status:
            return seen
        assert asyncio.get_running_loop().time() < deadline, f"still {payload['status']}"
        await asyncio.sleep(0.02)


def _running(payload: dict) -> set[str]:
    return {step["title"] for step in payload["steps"] if step["status"] == "running"}


def _status_by_title(payload: dict) -> dict[str, str]:
    return {step["title"]: step["status"] for step in payload["steps"]}


def _fan_out_script(answers: dict[str, str]):
    """Answer each node from its own instruction, whatever order the requests arrive in."""

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        for title, answer in answers.items():
            if title in asked:
                return {"content": answer}
        return {"content": "做完了"}

    return script


def test_two_branches_of_one_fan_out_really_overlap(make_backend, upstream) -> None:
    """A -> (B, C) -> D: B and C are on the canvas as running at the same time, D after both."""
    upstream.script = [
        _fan_out_script({"入口": "入口做完", "左支路": "左支路做完", "右支路": "右支路做完", "汇合": "汇合做完"})
    ]
    # Each reply takes ~0.6 s (3 chunks), so two sequential branches would never look alike.
    upstream.chunk_delay = 0.2
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "start", "title": "入口", "instruction": "入口"},
                    {"id": "left", "title": "左", "instruction": "左支路"},
                    {"id": "right", "title": "右", "instruction": "右支路"},
                    {"id": "join", "title": "汇合", "instruction": "汇合"},
                ],
                edges=[
                    {"from": "start", "to": "left"},
                    {"from": "start", "to": "right"},
                    {"from": "left", "to": "join"},
                    {"from": "right", "to": "join"},
                ],
            )
            started = await _run(client, workflow["id"])
            seen = await _sample(client, started["workflow_run"]["id"])

            # Both arms were in flight at once: one payload named both as running.
            assert any({"左", "右"} <= _running(payload) for payload in seen), [
                sorted(_running(payload)) for payload in seen
            ]
            # The join never started while a parent was still going.
            for payload in seen:
                states = _status_by_title(payload)
                if states["汇合"] != "pending":
                    assert states["左"] in ("done", "failed", "skipped"), states
                    assert states["右"] in ("done", "failed", "skipped"), states
            done = seen[-1]
            assert _status_by_title(done) == {
                "入口": "done",
                "左": "done",
                "右": "done",
                "汇合": "done",
            }
            # Each node ran exactly once, and the join's answer came last.
            assert len(upstream.requests) == 4
            assert _by_title(done, "汇合")["output"] == "汇合做完"

    asyncio.run(body())


def test_no_more_than_the_worker_cap_runs_at_once(make_backend, upstream) -> None:
    """Five ready branches: four start, the fifth waits for a slot and then runs."""
    upstream.script = [_fan_out_script({"入口": "入口做完"})]
    upstream.chunk_delay = 0.2
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            arms = [f"支路{number}" for number in range(1, 6)]
            workflow = await _create(
                client,
                steps=[{"id": "start", "title": "入口", "instruction": "入口"}]
                + [
                    {"id": f"b{number}", "title": title, "instruction": title}
                    for number, title in enumerate(arms, start=1)
                ],
                edges=[{"from": "start", "to": f"b{number}"} for number in range(1, 6)],
            )
            started = await _run(client, workflow["id"])
            seen = await _sample(client, started["workflow_run"]["id"])

            widest = max(len(_running(payload)) for payload in seen)
            assert widest == rules.MAX_PARALLEL_STEPS, widest
            # The arm that had to wait was pending while four others ran, then it ran too.
            waiting = next(
                title
                for title in arms
                if any(
                    _status_by_title(payload)[title] == "pending"
                    and len(_running(payload)) == rules.MAX_PARALLEL_STEPS
                    for payload in seen
                )
            )
            assert any(waiting in _running(payload) for payload in seen)
            assert all(state == "done" for state in _status_by_title(seen[-1]).values())

    asyncio.run(body())


def test_stop_cancels_every_running_branch(make_backend, upstream) -> None:
    """The stop button ends the walk at once: neither arm gets to finish its reply."""
    upstream.script = [_fan_out_script({"入口": "入口做完"})]
    upstream.chunk_delay = 0.4
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "start", "title": "入口", "instruction": "入口"},
                    {"id": "left", "title": "左", "instruction": "左支路"},
                    {"id": "right", "title": "右", "instruction": "右支路"},
                ],
                edges=[
                    {"from": "start", "to": "left"},
                    {"from": "start", "to": "right"},
                ],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            # Run until both arms are really streaming.
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                walk = (await client.get(f"/workflow-runs/{workflow_run_id}")).json()
                if {"左", "右"} <= _running(walk):
                    break
                assert asyncio.get_running_loop().time() < deadline, walk
                await asyncio.sleep(0.02)

            stopped = await client.post(f"/workflow-runs/{workflow_run_id}/stop")
            assert stopped.status_code == 200, stopped.text
            walk = await _wait(client, workflow_run_id, status="stopped")

            states = _status_by_title(walk)
            assert states["左"] != "done" and states["右"] != "done", states
            # A cancelled node goes back to pending: the panel never shows a node that is gone.
            assert states["左"] == "pending" and states["右"] == "pending", states

    asyncio.run(body())


def test_a_question_in_one_branch_does_not_hold_up_the_other(make_backend, upstream) -> None:
    """The question parks its own node; the branch beside it runs to completion first."""
    upstream.script = [
        _fan_out_script({"收集": "资料齐了", "整理": "整理好了"})
    ]
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "collect", "title": "收集", "instruction": "收集"},
                    {"id": "ask", "title": "要哪几天", "instruction": "要哪几天的数据？",
                     "kind": "ask_user"},
                    {"id": "work", "title": "整理", "instruction": "整理"},
                ],
                edges=[
                    {"from": "collect", "to": "ask"},
                    {"from": "collect", "to": "work"},
                ],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            parked = await _wait(client, workflow_run_id, status="waiting_user")

            states = _status_by_title(parked)
            assert states["要哪几天"] == "waiting", states
            assert states["整理"] == "done", states

            answered = await client.post(
                f"/workflow-runs/{workflow_run_id}/answer", json={"input": "最近三天"}
            )
            assert answered.status_code == 200, answered.text
            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "要哪几天")["output"] == "最近三天"
            assert _by_title(done, "整理")["status"] == "done"

    asyncio.run(body())


def test_pause_lets_the_running_branches_finish_and_starts_nothing_new(
    make_backend, upstream
) -> None:
    """The button stops at a node boundary: both arms settle, the join waits for continue."""
    upstream.script = [
        _fan_out_script({"入口": "入口做完", "左支路": "左支路做完", "右支路": "右支路做完", "汇合": "汇合做完"})
    ]
    upstream.chunk_delay = 0.2
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "start", "title": "入口", "instruction": "入口"},
                    {"id": "left", "title": "左", "instruction": "左支路"},
                    {"id": "right", "title": "右", "instruction": "右支路"},
                    {"id": "join", "title": "汇合", "instruction": "汇合"},
                ],
                edges=[
                    {"from": "start", "to": "left"},
                    {"from": "start", "to": "right"},
                    {"from": "left", "to": "join"},
                    {"from": "right", "to": "join"},
                ],
            )
            started = await _run(client, workflow["id"])
            workflow_run_id = started["workflow_run"]["id"]
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                walk = (await client.get(f"/workflow-runs/{workflow_run_id}")).json()
                if {"左", "右"} <= _running(walk):
                    break
                assert asyncio.get_running_loop().time() < deadline, walk
                await asyncio.sleep(0.02)

            paused = await client.post(f"/workflow-runs/{workflow_run_id}/pause")
            assert paused.status_code == 200, paused.text
            parked = await _wait(client, workflow_run_id, status="paused")

            states = _status_by_title(parked)
            # Neither arm was cut in half: a pause waits for the node, it does not interrupt it.
            assert states["左"] == "done" and states["右"] == "done", states
            assert states["汇合"] == "pending", states

            resumed = await client.post(f"/workflow-runs/{workflow_run_id}/resume")
            assert resumed.status_code == 200, resumed.text
            done = await _wait(client, workflow_run_id, status="done")
            assert _by_title(done, "汇合")["output"] == "汇合做完"

    asyncio.run(body())


def test_a_failed_branch_still_lets_its_sibling_finish(make_backend, upstream) -> None:
    """One arm's model call fails; the other arm's work stands and the join is skipped."""

    def script(requests, index):
        asked = _last_user(requests[index - 1])
        if "左支路" in asked:
            return {"error_text": "模型连不上"}
        return {"content": "右支路做完" if "右支路" in asked else "做完了"}

    upstream.script = [script]
    upstream.chunk_delay = 0.2
    backend = make_backend()

    async def body() -> None:
        async with client_for(backend) as client:
            workflow = await _create(
                client,
                steps=[
                    {"id": "start", "title": "入口", "instruction": "入口"},
                    {"id": "left", "title": "左", "instruction": "左支路"},
                    {"id": "right", "title": "右", "instruction": "右支路"},
                    {"id": "join", "title": "汇合", "instruction": "汇合"},
                ],
                edges=[
                    {"from": "start", "to": "left"},
                    {"from": "start", "to": "right"},
                    {"from": "left", "to": "join"},
                    {"from": "right", "to": "join"},
                ],
            )
            started = await _run(client, workflow["id"])
            walk = await _wait(client, started["workflow_run"]["id"], status="failed")

            states = _status_by_title(walk)
            assert states["左"] == "failed", states
            # The sibling was not cancelled: its work had already been done.
            assert states["右"] == "done", states
            # The join is downstream of the failure and is marked as such.
            assert states["汇合"] == "skipped", states
            assert "模型连不上" in walk["summary"]

    asyncio.run(body())
