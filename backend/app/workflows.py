"""Workflows as graphs: nodes to run, edges that say what may follow what.

A workflow is a small graph. Nodes do work (``do``), ask the user (``ask_user``) or pick a path
(``branch``); edges say which node may run after which, and a ``branch`` node's edges carry the
choice they belong to. The engine — not the model — owns the walk: it settles the graph one
node at a time, decides what became unreachable when a choice went the other way, and waits at
a join until every parent has settled. This module holds the pure rules (what a node may be,
what makes a graph valid, how a prompt describes the graph) and leaves storage and execution
to :mod:`app.store.workflows` and :mod:`app.engine_workflows`.

Loops are explicit. An edge that goes back to an earlier node carries ``max_loops``: taking it
runs the nodes between its target and its source again, at most that many times. Every other
edge is a forward edge, and the forward edges alone must form a DAG — so the order of a round,
the joins and the dead arms all keep their DAG meaning, and a loop can never spin forever.

One worker: ready nodes run one after another in the order the plan lists them. The graph is
real (fan-out, joins, choices, dead branches, loops), but two branches never run at the same
instant — a run owns exactly one conversation, and one stream per conversation is the
invariant the rest of the engine is built on.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1_024
MAX_NODES = 30
MAX_EDGES = 60
MAX_WORKFLOWS_PER_BOT = 20
MAX_TITLE_CHARS = 120
MAX_INSTRUCTION_CHARS = 4_000
MAX_CHOICES = 4
MAX_EDGE_LABEL_CHARS = 24
# How many extra rounds one loop edge may send the walk back for.
MAX_LOOPS = 20
# How many nodes of one walk run at the same time. A ready node beyond this many is what the
# panel calls 「排队」; the web copy of this number is ``PARALLEL_STEPS`` in lib/workflow.ts.
MAX_PARALLEL_STEPS = 4
# Where a node sits on the panel's canvas; nulls fall back to the automatic layout.
MAX_COORDINATE = 20_000

NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# What a node does.
DO = "do"
ASK_USER = "ask_user"
BRANCH = "branch"
STEP_KINDS: tuple[str, ...] = (DO, ASK_USER, BRANCH)
STEP_KIND_LABELS = {DO: "执行", ASK_USER: "问你", BRANCH: "分支"}

# ``auto`` runs the node straight away; ``confirm`` shows a card and waits for the user first.
GATE_AUTO = "auto"
GATE_CONFIRM = "confirm"
GATES: tuple[str, ...] = (GATE_AUTO, GATE_CONFIRM)

# What a failed node does to the rest of the run.
FAIL_STOP = "stop"
FAIL_RETRY = "retry"
FAIL_SKIP = "skip"
FAIL_POLICIES: tuple[str, ...] = (FAIL_STOP, FAIL_RETRY, FAIL_SKIP)
FAIL_LABELS = {FAIL_STOP: "停下", FAIL_RETRY: "重试一次", FAIL_SKIP: "跳过"}

# A run's lifecycle. ``waiting_user`` and ``paused`` are the two states a run can sit in
# until something of the user's moves it; both survive a restart.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting_user"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
LIVE_STATUSES: tuple[str, ...] = (STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING, STATUS_PAUSED)
FINISHED_STATUSES: tuple[str, ...] = (STATUS_DONE, STATUS_FAILED, STATUS_STOPPED)
# A queued or running walk is one the process was in the middle of when it stopped.
MID_STATUSES: tuple[str, ...] = (STATUS_QUEUED, STATUS_RUNNING)

# One node's place in a run.
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_WAITING = "waiting"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
SETTLED: tuple[str, ...] = (STEP_DONE, STEP_FAILED, STEP_SKIPPED)


class WorkflowInvalid(ValueError):
    """A workflow the user or a bot asked for that cannot be stored as written."""


# ---------------------------------------------------------------------- validation


def validate_name(raw: str) -> str:
    name = " ".join(str(raw).split())
    if not name:
        raise WorkflowInvalid("工作流需要一个名字。")
    if len(name) > MAX_NAME_CHARS:
        raise WorkflowInvalid(f"工作流名字最多 {MAX_NAME_CHARS} 个字符。")
    return name


def validate_description(raw: str) -> str:
    description = str(raw or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise WorkflowInvalid(f"工作流说明最多 {MAX_DESCRIPTION_CHARS} 个字符。")
    return description


def _pick(value: Any, allowed: Sequence[str], default: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if text not in allowed:
        raise WorkflowInvalid(f"{field}只能是 {'、'.join(allowed)} 之一。")
    return text


def _coordinate(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0 or number > MAX_COORDINATE:  # NaN or out of range
        return None
    return round(number, 1)


def normalize_graph(raw_nodes: Any, raw_edges: Any = None) -> tuple[list[dict], list[dict]]:
    """The nodes and edges as stored, validated: a DAG of forward edges, plus bounded loops.

    With no edges, the plan is read as a chain in node order — which is what a straight list of
    steps has always meant, so an old workflow (or a bot writing a list of lines) keeps working.
    Node ids come from the input when they are usable, so dragging a node around the canvas
    never renames it; otherwise they are positional (``n1``…).
    """
    nodes = _nodes(raw_nodes)
    edges = _edges(raw_edges, nodes)
    _validate(nodes, edges)
    return nodes, edges


def _nodes(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise WorkflowInvalid("工作流至少要有一个步骤。")
    if len(raw) > MAX_NODES:
        raise WorkflowInvalid(f"一个工作流最多 {MAX_NODES} 个节点。")
    nodes: list[dict] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        node = _node(item, position)
        while node["id"] in seen:  # a repeated id would silently merge two nodes
            node["id"] = f"{node['id']}_{position}"[:32]
        seen.add(node["id"])
        nodes.append(node)
    return nodes


def _node(item: Any, position: int) -> dict:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            raise WorkflowInvalid(f"第 {position} 个节点是空的。")
        item = {"instruction": text}
    if not isinstance(item, dict):
        raise WorkflowInvalid(f"第 {position} 个节点格式不对，需要 title / instruction 字段。")
    raw_instruction = str(item.get("instruction") or "").strip()
    if not raw_instruction:
        raise WorkflowInvalid(f"第 {position} 个节点缺少 instruction（这一步要做什么）。")
    if len(raw_instruction) > MAX_INSTRUCTION_CHARS:
        raise WorkflowInvalid(
            f"第 {position} 个节点的 instruction 超过 {MAX_INSTRUCTION_CHARS} 个字符。"
        )
    title = " ".join(str(item.get("title") or "").split())
    if not title:
        title = " ".join(raw_instruction.split())[:40]
    if len(title) > MAX_TITLE_CHARS:
        raise WorkflowInvalid(f"第 {position} 个节点的标题最多 {MAX_TITLE_CHARS} 个字符。")
    kind = _pick(item.get("kind"), STEP_KINDS, DO, f"第 {position} 个节点的种类")
    gate = _pick(item.get("gate"), GATES, GATE_AUTO, f"第 {position} 个节点的确认方式")
    if kind != DO:
        # A question already waits for the user, and a branch is decided, not confirmed.
        gate = GATE_AUTO
    node = {
        "id": str(item.get("id") or f"n{position}"),
        "title": title,
        "kind": kind,
        "instruction": raw_instruction,
        "gate": gate,
        "on_fail": _pick(
            item.get("on_fail"), FAIL_POLICIES, FAIL_STOP, f"第 {position} 个节点的失败处理"
        ),
        "who": None,
        "x": _coordinate(item.get("x")),
        "y": _coordinate(item.get("y")),
    }
    if not NODE_ID.match(node["id"]):
        node["id"] = f"n{position}"
    if kind == BRANCH:
        node["choices"] = _choices(item.get("choices"), position)
    return node


def _choices(raw: Any, position: int) -> list[str]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise WorkflowInvalid(f"第 {position} 个节点是分支，需要至少两个 choices（走哪条）。")
    if len(raw) > MAX_CHOICES:
        raise WorkflowInvalid(f"一个分支最多 {MAX_CHOICES} 个 choices。")
    choices: list[str] = []
    for item in raw:
        label = " ".join(str(item or "").split())
        if not label:
            raise WorkflowInvalid(f"第 {position} 个节点的 choices 里有空的一项。")
        if len(label) > MAX_EDGE_LABEL_CHARS:
            raise WorkflowInvalid(f"分支的选项最多 {MAX_EDGE_LABEL_CHARS} 个字符：{label}。")
        if label in choices:
            raise WorkflowInvalid(f"分支「{label}」出现了两次。")
        choices.append(label)
    return choices


def _edges(raw: Any, nodes: Sequence[dict]) -> list[dict]:
    if raw is None:
        # A straight list of steps: chain them in the order they were given.
        return edges_from_chain(nodes)
    if not isinstance(raw, list):
        raise WorkflowInvalid("edges 需要是一个列表。")
    if not raw:
        return []
    if len(raw) > MAX_EDGES:
        raise WorkflowInvalid(f"一个工作流最多 {MAX_EDGES} 条连线。")
    by_id = {node["id"]: node for node in nodes}
    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowInvalid("每条连线都需要 from 和 to。")
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        if source not in by_id:
            raise WorkflowInvalid(f"连线指向了不存在的节点：{source or '（空）'}。")
        if target not in by_id:
            raise WorkflowInvalid(f"连线指向了不存在的节点：{target or '（空）'}。")
        if source == target:
            raise WorkflowInvalid(
                f"节点「{by_id[source]['title']}」不能连到自己；要重复做，就连回它前面的节点并设循环次数。"
            )
        if (source, target) in seen:
            continue
        seen.add((source, target))
        when = item.get("when")
        label = " ".join(str(when or "").split()) or None
        if label is not None:
            if by_id[source]["kind"] != BRANCH:
                raise WorkflowInvalid(
                    f"只有分支节点能带条件连线，「{by_id[source]['title']}」不是分支。"
                )
            if label not in by_id[source].get("choices", []):
                raise WorkflowInvalid(
                    f"「{by_id[source]['title']}」没有「{label}」这个选项。"
                )
        edge: dict = {"from": source, "to": target, "when": label}
        loops = _max_loops(item.get("max_loops"), by_id[source], by_id[target])
        if loops is not None:
            edge["max_loops"] = loops
        edges.append(edge)
    return edges


def _max_loops(raw: Any, source: dict, target: dict) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
        raise WorkflowInvalid(
            f"「{source['title']}」→「{target['title']}」的 max_loops 要是 1 到 {MAX_LOOPS} 的整数。"
        )
    count = int(raw)
    if not 1 <= count <= MAX_LOOPS:
        raise WorkflowInvalid(
            f"「{source['title']}」→「{target['title']}」最多循环 1 到 {MAX_LOOPS} 轮。"
        )
    return count


def _validate(nodes: Sequence[dict], edges: Sequence[dict]) -> None:
    """Forward edges form a DAG; every loop edge really goes back; nothing is stranded.

    A cycle made of forward edges has no bound, so it is refused with the nodes on it named;
    a loop edge whose target does not lead to its source is not a loop at all. Beyond that, a
    node nobody drew an edge for would simply never run, and a branch with no outgoing edge
    would silently end the walk.
    """
    titles = {node["id"]: node["title"] for node in nodes}
    forward = forward_edges(edges)
    if topological(nodes, forward) is None:
        cycle = " → ".join(titles[node_id] for node_id in find_cycle(nodes, forward) or [])
        raise WorkflowInvalid(
            f"连线成环了：{cycle}。要让它循环，给绕回去的那条线设置最多循环几轮（max_loops）。"
        )
    for edge in edges:
        if is_loop(edge) and edge["from"] not in descendants(nodes, forward, edge["to"]):
            raise WorkflowInvalid(
                f"「{titles[edge['from']]}」→「{titles[edge['to']]}」设了循环次数，"
                "但它没有绕回到前面的节点：去掉 max_loops，或者连回它前面的节点。"
            )
    parents = parents_of(nodes, edges)
    children = children_of(nodes, edges)
    if len(nodes) > 1:
        stranded = [
            node["title"]
            for node in nodes
            if not parents.get(node["id"]) and not children.get(node["id"])
        ]
        if stranded:
            raise WorkflowInvalid("这些节点没有任何连线：" + "、".join(stranded) + "。")
    for node in nodes:
        if node["kind"] == BRANCH and not children.get(node["id"]):
            raise WorkflowInvalid(f"分支「{node['title']}」还没有连出去。")


# ---------------------------------------------------------------------- graph shape


def is_loop(edge: Mapping[str, Any]) -> bool:
    """An edge that sends the walk back: it carries how many rounds it may cause."""
    return bool(edge.get("max_loops"))


def forward_edges(edges: Sequence[dict]) -> list[dict]:
    return [edge for edge in edges if not is_loop(edge)]


def edge_key(edge: Mapping[str, Any]) -> str:
    """One edge's identity; the panel uses the same string (``edgeKey``)."""
    return f"{edge['from']}->{edge['to']}#{edge.get('when') or ''}"


def parents_of(nodes: Sequence[dict], edges: Sequence[dict]) -> dict[str, list[dict]]:
    parents: dict[str, list[dict]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        parents[edge["to"]].append(edge)
    return parents


def children_of(nodes: Sequence[dict], edges: Sequence[dict]) -> dict[str, list[dict]]:
    children: dict[str, list[dict]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        children[edge["from"]].append(edge)
    return children


def topological(nodes: Sequence[dict], edges: Sequence[dict]) -> list[str] | None:
    """Node ids in dependency order, or ``None`` when the graph has a cycle."""
    index = {node["id"]: position for position, node in enumerate(nodes)}
    incoming = {node["id"]: 0 for node in nodes}
    for edge in edges:
        incoming[edge["to"]] += 1
    ready = sorted(
        (node_id for node_id, count in incoming.items() if count == 0), key=index.__getitem__
    )
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for edge in edges:
            if edge["from"] != current:
                continue
            incoming[edge["to"]] -= 1
            if incoming[edge["to"]] == 0:
                ready.append(edge["to"])
                ready.sort(key=index.__getitem__)
    return order if len(order) == len(nodes) else None


def find_cycle(nodes: Sequence[dict], edges: Sequence[dict]) -> list[str] | None:
    """One cycle as the node ids along it (first id repeated at the end), or ``None``."""
    children = children_of(nodes, edges)
    marks: dict[str, int] = {}  # 1 = on the stack, 2 = finished
    stack: list[str] = []

    def walk(node_id: str) -> list[str] | None:
        mark = marks.get(node_id)
        if mark == 2:
            return None
        if mark == 1:
            return [*stack[stack.index(node_id):], node_id]
        marks[node_id] = 1
        stack.append(node_id)
        for edge in children.get(node_id, []):
            found = walk(edge["to"])
            if found:
                return found
        stack.pop()
        marks[node_id] = 2
        return None

    for node in nodes:
        found = walk(node["id"])
        if found:
            return found
    return None


def descendants(nodes: Sequence[dict], edges: Sequence[dict], start: str) -> set[str]:
    """Every node reachable from ``start`` along ``edges``, ``start`` included."""
    children = children_of(nodes, edges)
    seen = {start}
    pending = [start]
    while pending:
        for edge in children.get(pending.pop(), []):
            if edge["to"] not in seen:
                seen.add(edge["to"])
                pending.append(edge["to"])
    return seen


def ancestors(nodes: Sequence[dict], edges: Sequence[dict], start: str) -> set[str]:
    """Every node that leads to ``start`` along ``edges``, ``start`` included."""
    parents = parents_of(nodes, edges)
    seen = {start}
    pending = [start]
    while pending:
        for edge in parents.get(pending.pop(), []):
            if edge["from"] not in seen:
                seen.add(edge["from"])
                pending.append(edge["from"])
    return seen


def loop_body(nodes: Sequence[dict], edges: Sequence[dict], loop: Mapping[str, Any]) -> list[str]:
    """The nodes one round of a loop resets: everything the loop's target leads to, forward.

    Not just the path back to the loop's source: an arm the last round skipped (its branch went
    the other way) or ran off the old result must be decided afresh in the new round, and the
    nodes after the source are still pending anyway — the loop is taken before they can start.
    """
    inside = descendants(nodes, forward_edges(edges), loop["to"])
    return [node["id"] for node in nodes if node["id"] in inside]


# ---------------------------------------------------------------------- execution


def loop_exhausted(edge: Mapping[str, Any], loops: Mapping[str, int]) -> bool:
    return is_loop(edge) and int(loops.get(edge_key(edge), 0)) >= int(edge["max_loops"])


def reachable_edges(
    node: dict,
    edges: Sequence[dict],
    states: Mapping[str, dict],
    loops: Mapping[str, int] | None = None,
) -> list[dict]:
    """The outgoing edges a settled node actually took.

    A branch takes only the edges whose ``when`` matches the choice it produced; every other
    edge is dead, and whatever hung off it will be skipped when its parents settle. A loop edge
    that has used up its rounds is dead too.
    """
    state = states.get(node["id"]) or {}
    if state.get("status") != STEP_DONE:
        return []
    outgoing = [
        edge
        for edge in edges
        if edge["from"] == node["id"] and not loop_exhausted(edge, loops or {})
    ]
    if node["kind"] != BRANCH:
        return outgoing
    taken = str(state.get("choice") or "").strip()
    return [edge for edge in outgoing if (edge.get("when") or "") == taken]


def available_choices(
    node: dict, edges: Sequence[dict], loops: Mapping[str, int]
) -> tuple[list[str], list[str]]:
    """A branch's choices it may still pick, and the ones whose loop has used up its rounds.

    A choice is closed when every edge it owns is a loop edge that is exhausted: picking it
    would lead nowhere. When that closes every choice the branch keeps them all, and the walk
    ends through the dead edges instead of failing on a choice it was never offered.
    """
    choices = list(node.get("choices") or [])
    closed: list[str] = []
    for choice in choices:
        owned = [edge for edge in edges if edge["from"] == node["id"] and edge.get("when") == choice]
        if owned and all(loop_exhausted(edge, loops) for edge in owned):
            closed.append(choice)
    open_choices = [choice for choice in choices if choice not in closed]
    return (open_choices, closed) if open_choices else (choices, [])


def parse_choice(output: str, choices: Sequence[str]) -> str | None:
    """Which branch choice a node's answer picked, or ``None``.

    An answer that is exactly one choice (punctuation and quotes aside) is that choice. Failing
    that, the choice named earliest wins, and among choices named at the same place the longest
    does — so 「不合格」 is never read as 「合格」, which it contains.
    """
    text = output.strip()
    if not text:
        return None
    bare = text.strip(" \t\n。.!！?？:：、，,「」『』\"'“”‘’`*").casefold()
    for choice in choices:
        if choice.casefold() == bare:
            return choice
    lowered = text.casefold()
    best: tuple[int, int] | None = None
    picked: str | None = None
    for choice in choices:
        at = lowered.find(choice.casefold())
        if at < 0:
            continue
        rank = (at, -len(choice))
        if best is None or rank < best:
            best, picked = rank, choice
    return picked


# ---------------------------------------------------------------------- prompts


def node_prompt(
    *,
    name: str,
    nodes: Sequence[dict],
    node: dict,
    answer: str = "",
    round_: int = 0,
    choices: tuple[list[str], list[str]] | None = None,
) -> str:
    """The user turn one node is given: where the walk is, and what this node must produce.

    The graph itself stays in the system prompt; this turn carries only the current node, so a
    long plan does not repeat its whole text into every request. A node a loop brought back is
    told which round it is on, and a branch is offered only the choices still open.
    """
    lines = [f"工作流「{name}」现在的节点：{node['title']}"]
    if round_ > 0:
        lines.append(
            f"这是循环回来的第 {round_ + 1} 轮：上一轮的结果在对话里，这一轮要在它的基础上改进，不要原样重复。"
        )
    lines.append(node["instruction"])
    if answer.strip():
        lines.append(f"用户在上一问的回答：{answer.strip()}")
    if node["kind"] == BRANCH:
        offered, closed = choices if choices is not None else (list(node.get("choices", [])), [])
        names = "、".join(f"「{choice}」" for choice in offered)
        lines.append(f"这一步要选一条路：{names}。只输出你选的那个词，不要解释。")
        if closed:
            spent = "、".join(f"「{choice}」" for choice in closed)
            lines.append(f"{spent}已经循环到上限，这次不能再选。")
    else:
        lines.append(
            "只完成这一步：做完就直接输出这一步的结论，不要替后面的步骤做决定；"
            "需要信息就用工具去取。"
        )
    return "\n\n".join(lines)


def question_text(node: dict) -> str:
    """An ``ask_user`` node's question, posted verbatim as the bot's own message."""
    return str(node["instruction"]).strip()


def plan_block(
    *,
    name: str,
    description: str,
    nodes: Sequence[dict],
    edges: Sequence[dict],
    states: dict[str, dict],
    current: str | None,
    loops: Mapping[str, int] | None = None,
) -> str:
    """The resident ``<workflow>`` block: the graph, with what is already settled.

    Statuses come from the run's node rows, so a resumed walk reads its own progress instead of
    being told to start over. A loop edge says how many rounds it has left.
    """
    children = children_of(nodes, edges)
    parents = parents_of(nodes, forward_edges(edges))
    titles = {node["id"]: node["title"] for node in nodes}
    used = loops or {}
    lines = ["<workflow>", f"你正在执行工作流「{name}」。"]
    if description:
        lines.append(f"工作流目的：{description}")
    lines.append("节点：")
    for node in nodes:
        state = states.get(node["id"]) or {}
        mark = {
            STEP_DONE: "已完成",
            STEP_FAILED: "失败",
            STEP_SKIPPED: "已跳过",
            STEP_RUNNING: "进行中",
            STEP_WAITING: "等你回答",
        }.get(str(state.get("status") or ""), "未开始")
        rounds = int(state.get("rounds") or 0)
        if rounds:
            mark += f"·第 {rounds + 1} 轮"
        kind = STEP_KIND_LABELS.get(node["kind"], node["kind"])
        # This node points; a sibling that runs at the same time is marked by its status, so
        # two branches side by side each see the other as 进行中 rather than as the current one.
        pointer = "→" if node["id"] == current or state.get("status") == STEP_RUNNING else " "
        outgoing = children.get(node["id"], [])
        nexts = "、".join(_edge_phrase(edge, titles, used) for edge in outgoing)
        incoming = "、".join(titles[edge["from"]] for edge in parents.get(node["id"], []))
        line = f"{pointer} [{kind}·{mark}] {node['title']}"
        if incoming:
            line += f" ← 接在 {incoming} 之后"
        if nexts:
            line += f" → 之后 {nexts}"
        lines.append(line)
    lines.append(
        "只做箭头指向的这个节点。同时进行中的节点是别的分支，由引擎另开一路在做，"
        "不要替它们做；别的分支和循环也由引擎决定，你不需要判断；不要重复已完成的节点。"
    )
    lines.append("</workflow>")
    return "\n".join(lines)


def _edge_phrase(edge: Mapping[str, Any], titles: Mapping[str, str], used: Mapping[str, int]) -> str:
    phrase = titles[edge["to"]]
    if edge.get("when"):
        phrase += f"（当「{edge['when']}」）"
    if is_loop(edge):
        taken = int(used.get(edge_key(edge), 0))
        phrase += f"（循环回去，已绕 {taken}/{edge['max_loops']} 轮）"
    return phrase


def pending_states(nodes: Sequence[dict]) -> list[dict]:
    """The fresh node rows one new run starts with, in plan order."""
    return [
        {
            "node_id": node["id"],
            "step_index": position,
            "title": node["title"],
            "kind": node["kind"],
            "status": STEP_PENDING,
            "attempts": 0,
            "approved": 0,
            "output": "",
            "choice": None,
            "message_id": None,
        }
        for position, node in enumerate(nodes)
    ]


def render_markdown(workflow: dict) -> str:
    """The plan as ``WORKFLOW.md``: what the panel offers to copy, and what a bot can read."""
    nodes = workflow.get("nodes") or workflow["steps"]
    edges = workflow.get("edges") or []
    children = children_of(nodes, edges)
    lines = [
        "---",
        f"name: {workflow['name']}",
        f"description: {workflow.get('description') or ''}",
        "---",
        "",
        f"# {workflow['name']}",
        "",
    ]
    for position, node in enumerate(nodes, start=1):
        kind = STEP_KIND_LABELS.get(node["kind"], node["kind"])
        lines.append(f"{position}. [{kind}] {node['title']}")
        lines.append(f"   {node['instruction']}")
        notes: list[str] = []
        if node["gate"] == GATE_CONFIRM:
            notes.append("开始前要你确认")
        if node.get("on_fail") and node["on_fail"] != FAIL_STOP:
            notes.append(f"失败{FAIL_LABELS.get(node['on_fail'], node['on_fail'])}")
        if notes:
            lines.append(f"   （{'，'.join(notes)}）")
        for edge in children.get(node["id"], []):
            target = next(item for item in nodes if item["id"] == edge["to"])
            label = f"当「{edge['when']}」" if edge.get("when") else ""
            if is_loop(edge):
                lines.append(f"   ↺ {label}回到 {target['title']}（最多循环 {edge['max_loops']} 轮）")
            else:
                lines.append(f"   → {label}接着 {target['title']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def edges_from_chain(nodes: Sequence[dict]) -> list[dict]:
    """One chain through ``nodes``, used by every migration from a linear plan."""
    return [
        {"from": nodes[index]["id"], "to": nodes[index + 1]["id"], "when": None}
        for index in range(len(nodes) - 1)
    ]


def node_ids(nodes: Iterable[dict]) -> list[str]:
    return [node["id"] for node in nodes]
