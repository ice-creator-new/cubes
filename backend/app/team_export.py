"""Team export and import as one Markdown file.

The file is the team's shape, not its history: members (name, instructions, appearance, the
picked provider name and model, tool permissions, token budget, turned-off skill names),
groups and their memberships, workflows with their graphs, and the names of the skills that
exist. Messages, memories, API keys, tokens and webhook secrets never enter it.

`POST /team/import` reads the same file. It previews what would happen without writing, and
with ``apply`` it creates the missing bots, groups and workflows. A name already taken in the
target workspace gets a 「（2）」 suffix instead of overwriting anyone; the supervisor is only
changed when the caller explicitly asks for it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import workflows as workflow_rules
from .appearance import parse_appearance
from .errors import AgentNameTaken
from .models import MAX_TOOL_TEXT_CHARS
from .permissions import MODES

FORMAT_VERSION = 1
MEMBER_HEADING = "成员"
GROUP_HEADING = "分组"
WORKFLOW_HEADING = "工作流"
SKILL_HEADING = "技能"

_BLOCK = re.compile(r"^##\s+(.+?)\s*$\n+```json\s*\n(.*?)\n```", re.MULTILINE | re.DOTALL)


def _suffix_name(base: str, n: int) -> str:
    return base if n == 1 else f"{base}（{n}）"


def _unique(base: str, taken: set[str]) -> str:
    """``base``, else ``base（2）``, ``base（3）``… until it is free in ``taken``."""
    key = base.strip().casefold()
    if key not in taken:
        taken.add(key)
        return base.strip()
    n = 2
    while f"{key}（{n}）" in taken:
        n += 1
    taken.add(f"{key}（{n}）")
    return f"{base.strip()}（{n}）"


def _member_entry(agent: dict, providers: dict[str, dict]) -> dict[str, Any]:
    """One agent as the export carries it: never a key, only the provider's name."""
    permissions = agent.get("tool_permissions")
    if isinstance(permissions, dict):
        permissions = {k: v for k, v in permissions.items() if k != "skills_disabled"}
    else:
        permissions = None
    disabled = []
    if isinstance(agent.get("tool_permissions"), dict):
        disabled = list(agent["tool_permissions"].get("skills_disabled") or [])
    model = None
    ref = agent.get("model_ref")
    if isinstance(ref, dict) and ref.get("provider_id"):
        provider = providers.get(str(ref["provider_id"]))
        if provider is not None:
            model = {"provider": provider["name"], "model": ref.get("model")}
            if ref.get("effort"):
                model["effort"] = ref["effort"]
    return {
        "name": agent["name"],
        "kind": agent["kind"],
        "instructions": agent["instructions"],
        "appearance": agent.get("appearance"),
        "model": model,
        "tool_permissions": permissions,
        "token_budget": agent.get("token_budget"),
        "permission_mode": agent.get("permission_mode"),
        "skills_disabled": disabled,
    }


def export_payload(store: Any) -> dict[str, list[dict] | list[str]]:
    """The team's shape, ready to be written as Markdown sections."""
    providers = {provider["id"]: provider for provider in store.list_providers()}
    agents = store.list_agents()
    names = {agent["id"]: agent["name"] for agent in agents}
    conversations = [
        conversation
        for conversation in store.list_conversations()
        if conversation["kind"] == "group"
    ]
    workflows = []
    for workflow in store.list_workflows():
        workflows.append(
            {
                "name": workflow["name"],
                "description": workflow["description"],
                "owner": names.get(workflow["owner_agent_id"], ""),
                "enabled": bool(workflow["enabled"]),
                "nodes": workflow["steps"],
                "edges": workflow["edges"],
            }
        )
    return {
        "members": [_member_entry(agent, providers) for agent in agents],
        "groups": [
            {
                "title": conversation["title"],
                "purpose": conversation["purpose"],
                "members": [
                    names.get(agent_id, "")
                    for agent_id in conversation["member_ids"]
                    if names.get(agent_id)
                ],
            }
            for conversation in conversations
        ],
        "workflows": workflows,
        "skills": [skill["name"] for skill in store.list_skills()],
    }


def export_markdown(store: Any) -> str:
    """One Markdown file: front matter, then a JSON section per kind of thing."""
    payload = export_payload(store)
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "---",
        f"cubes_team: {FORMAT_VERSION}",
        f"exported_at: {exported_at}",
        "---",
        "",
        "# Cubes 团队",
        "",
        "这是团队的结构（成员、分组、工作流和技能名），不含聊天记录、记忆、密钥或令牌。",
        "",
        f"## {MEMBER_HEADING}",
        "",
        "```json",
        json.dumps(payload["members"], ensure_ascii=False, indent=2),
        "```",
        "",
        f"## {GROUP_HEADING}",
        "",
        "```json",
        json.dumps(payload["groups"], ensure_ascii=False, indent=2),
        "```",
        "",
        f"## {WORKFLOW_HEADING}",
        "",
        "```json",
        json.dumps(payload["workflows"], ensure_ascii=False, indent=2),
        "```",
        "",
        f"## {SKILL_HEADING}",
        "",
        "```json",
        json.dumps({"names": payload["skills"]}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def parse_markdown(text: str) -> dict[str, Any]:
    """Read the sections back; an unreadable block is a Chinese error the route returns."""
    if not text.strip():
        raise ValueError("导入文件是空的。")
    sections: dict[str, Any] = {}
    for heading, body in _BLOCK.findall(text):
        try:
            sections[heading.strip()] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"「{heading.strip()}」这一节的 JSON 无效：{exc.msg}。") from exc
    if MEMBER_HEADING not in sections:
        raise ValueError(f"导入文件里没有「{MEMBER_HEADING}」一节。")
    members = sections.get(MEMBER_HEADING)
    if not isinstance(members, list) or not members:
        raise ValueError("导入文件里没有任何成员。")
    groups = sections.get(GROUP_HEADING) or []
    workflows = sections.get(WORKFLOW_HEADING) or []
    skills = sections.get(SKILL_HEADING) or {}
    if not isinstance(groups, list) or not isinstance(workflows, list):
        raise ValueError("「分组」和「工作流」必须是列表。")
    if isinstance(skills, dict):
        skill_names = [str(name) for name in skills.get("names") or []]
    else:
        skill_names = [str(name) for name in skills] if isinstance(skills, list) else []
    return {"members": members, "groups": groups, "workflows": workflows, "skills": skill_names}


def _clean_member(raw: dict) -> dict[str, Any]:
    """One imported member with only the fields the file is allowed to set."""
    name = " ".join(str(raw.get("name") or "").split())
    if not name:
        raise ValueError("有一个成员没有名字。")
    kind = str(raw.get("kind") or "member")
    if kind not in ("supervisor", "member"):
        kind = "member"
    permissions = raw.get("tool_permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else None
    disabled = [str(item) for item in raw.get("skills_disabled") or [] if str(item).strip()]
    if permissions is None and disabled:
        permissions = {}
    if permissions is not None and disabled:
        permissions["skills_disabled"] = disabled
    budget = raw.get("token_budget")
    budget = dict(budget) if isinstance(budget, dict) else None
    model = raw.get("model")
    model = dict(model) if isinstance(model, dict) and model.get("provider") else None
    appearance = raw.get("appearance")
    if appearance is not None:
        appearance = parse_appearance(json.dumps(appearance, ensure_ascii=False))
    instructions = str(raw.get("instructions") or "")[:MAX_TOOL_TEXT_CHARS]
    # ``None`` means "follow the workspace's mode", the same as an agent that never picked one.
    mode = raw.get("permission_mode")
    mode = mode if mode in MODES else None
    return {
        "name": name,
        "kind": kind,
        "instructions": instructions,
        "appearance": appearance,
        "model": model,
        "tool_permissions": permissions,
        "token_budget": budget,
        "permission_mode": mode,
    }


def _model_ref(store: Any, model: dict | None, warnings: list[str], who: str) -> dict | None:
    if model is None:
        return None
    provider_name = str(model.get("provider") or "")
    provider = next(
        (item for item in store.list_providers() if str(item["name"]).casefold() == provider_name.casefold()),
        None,
    )
    if provider is None:
        warnings.append(f"「{who}」选的模型服务「{provider_name}」不在这里，已改为跟随默认模型。")
        return None
    ref: dict[str, Any] = {"provider_id": provider["id"], "model": str(model.get("model") or "")}
    if model.get("effort"):
        ref["effort"] = str(model["effort"])
    return ref


def preview_import(
    store: Any, text: str, *, overwrite_supervisor: bool = False
) -> dict[str, Any]:
    """Plan the import without writing: what would be created, renamed, skipped or missing."""
    data = parse_markdown(text)
    warnings: list[str] = []
    supervisor = store.supervisor()
    agents = {agent["name"].casefold(): agent for agent in store.list_agents()}
    taken = set(agents)
    planned: list[dict] = []
    final_by_name: dict[str, str] = {}
    for raw in data["members"]:
        member = _clean_member(raw)
        original = member["name"]
        existing = agents.get(original.casefold())
        if member["kind"] == "supervisor" or (existing and existing["kind"] == "supervisor"):
            if supervisor is None:  # pragma: no cover - bootstrap always creates it
                warnings.append("这个工作区没有主管，无法导入主管设置。")
                planned.append({"name": original, "final_name": original, "kind": "supervisor", "action": "skip"})
                continue
            if not overwrite_supervisor:
                warnings.append(
                    f"主管「{supervisor['name']}」保持原样；需要覆盖主管设置时请勾选「覆盖主管」。"
                )
                planned.append(
                    {"name": original, "final_name": supervisor["name"], "kind": "supervisor", "action": "skip"}
                )
            else:
                planned.append(
                    {"name": original, "final_name": original, "kind": "supervisor", "action": "update"}
                )
            final_by_name[original.casefold()] = supervisor["name"]
            continue
        final = _unique(original, taken)
        action = "rename" if final != original else "create"
        planned.append({"name": original, "final_name": final, "kind": "member", "action": action})
        final_by_name[original.casefold()] = final

    group_titles = {
        conversation["title"].casefold()
        for conversation in store.list_conversations()
        if conversation["kind"] == "group"
    }
    groups: list[dict] = []
    used_titles = set(group_titles)
    for raw in data["groups"]:
        title = " ".join(str(raw.get("title") or "").split())
        if not title:
            groups.append({"title": "", "final_title": "", "action": "skip", "members": [], "purpose": ""})
            continue
        final_title = _unique(title, used_titles)
        groups.append(
            {
                "title": title,
                "final_title": final_title,
                "action": "rename" if final_title != title else "create",
                "members": [str(name) for name in raw.get("members") or []],
                "purpose": str(raw.get("purpose") or ""),
            }
        )

    workflow_names = {workflow["name"].casefold() for workflow in store.list_workflows()}
    used_workflows = set(workflow_names)
    workflows: list[dict] = []
    for raw in data["workflows"]:
        name = " ".join(str(raw.get("name") or "").split())
        if not name:
            workflows.append({"name": "", "final_name": "", "action": "skip"})
            continue
        final_name = _unique(name, used_workflows)
        workflows.append(
            {
                "name": name,
                "final_name": final_name,
                "action": "rename" if final_name != name else "create",
            }
        )

    available = {skill["name"] for skill in store.list_skills()}
    missing = [name for name in data["skills"] if name not in available]
    if missing:
        warnings.append(
            "这些技能只导出了名字，本机没有同名技能，导入不会重建它们：" + "、".join(missing)
        )
    return {
        "applied": False,
        "members": planned,
        "groups": groups,
        "workflows": workflows,
        "skills": {"names": data["skills"], "missing": missing},
        "warnings": warnings,
        "_data": data,
        "_final_by_name": final_by_name,
    }


def apply_import(
    store: Any, text: str, *, overwrite_supervisor: bool = False
) -> dict[str, Any]:
    """Create everything the preview planned; return the same shape with ``applied: true``."""
    plan = preview_import(store, text, overwrite_supervisor=overwrite_supervisor)
    data = plan.pop("_data")
    final_by_name: dict[str, str] = plan.pop("_final_by_name")
    warnings: list[str] = plan["warnings"]
    supervisor = store.supervisor()
    members_by_name = {agent["name"]: agent for agent in store.list_agents()}

    for raw, decision in zip(data["members"], plan["members"]):
        member = _clean_member(raw)
        if decision["action"] == "skip":
            continue
        ref = _model_ref(store, member["model"], warnings, decision["final_name"])
        if decision["action"] == "update":
            if supervisor is None:  # pragma: no cover
                continue
            changes: dict[str, Any] = {
                "instructions": member["instructions"],
                "appearance": member["appearance"],
                "model_ref": ref,
                "token_budget": member["token_budget"],
                "tool_permissions": member["tool_permissions"],
                "permission_mode": member["permission_mode"],
            }
            if member["name"] != supervisor["name"]:
                changes["name"] = member["name"]
            try:
                store.update_agent(supervisor["id"], **changes)
            except AgentNameTaken:
                changes.pop("name", None)
                store.update_agent(supervisor["id"], **changes)
                warnings.append(f"主管改成「{member['name']}」时名字已被占用，保留了原名。")
            continue
        try:
            created = store.create_agent(
                name=decision["final_name"], instructions=member["instructions"]
            )
        except AgentNameTaken:
            warnings.append(f"成员「{decision['final_name']}」的名字已被占用，跳过了这一位。")
            continue
        store.update_agent(
            created["id"],
            appearance=member["appearance"],
            model_ref=ref,
            token_budget=member["token_budget"],
            tool_permissions=member["tool_permissions"],
            permission_mode=member["permission_mode"],
        )
        members_by_name[created["name"]] = store.get_agent(created["id"]) or created

    def member_id(name: str) -> str | None:
        final = final_by_name.get(name.casefold(), name)
        agent = members_by_name.get(final)
        return None if agent is None else agent["id"]

    for decision in plan["groups"]:
        if decision["action"] == "skip":
            continue
        ids: list[str] = []
        for name in decision["members"]:
            agent_id = member_id(str(name))
            if agent_id is None:
                warnings.append(f"分组「{decision['final_title']}」里的「{name}」找不到，已略过。")
                continue
            if agent_id not in ids:
                ids.append(agent_id)
        if supervisor is not None and supervisor["id"] not in ids:
            ids.insert(0, supervisor["id"])
        if not ids:
            warnings.append(f"分组「{decision['final_title']}」没有任何成员，没有创建。")
            continue
        store.create_conversation(
            kind="group",
            title=decision["final_title"],
            purpose=decision["purpose"] or "从团队文件导入的分组",
            member_ids=ids,
        )

    for raw, decision in zip(data["workflows"], plan["workflows"]):
        if decision["action"] == "skip":
            continue
        try:
            nodes, edges = workflow_rules.normalize_graph(raw.get("nodes"), raw.get("edges"))
        except workflow_rules.WorkflowInvalid as exc:
            warnings.append(f"工作流「{decision['final_name']}」没有导入：{exc}")
            continue
        owner_id = member_id(str(raw.get("owner") or ""))
        if owner_id is None:
            owner_id = supervisor["id"] if supervisor is not None else None
            warnings.append(f"工作流「{decision['final_name']}」的负责人不在这里，已交给主管。")
        if owner_id is None:  # pragma: no cover
            continue
        store.create_workflow(
            name=decision["final_name"],
            description=str(raw.get("description") or ""),
            owner_agent_id=owner_id,
            created_by="团队导入",
            nodes=nodes,
            edges=edges,
            enabled=bool(raw.get("enabled", True)),
        )

    plan["applied"] = True
    plan["warnings"] = warnings
    return plan
