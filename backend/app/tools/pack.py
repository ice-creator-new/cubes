"""The toolkit pack as registry handlers: one adapter, no table of its own.

``ToolSpec`` already describes a tool completely; this module only wraps each spec in the
handler shape so the registry can offer, label and run it like any other family. The live
store rides along because a pack's own rules (a private chat, a paused switch) can only be
answered with it.
"""

from __future__ import annotations

from typing import Any

import json

from ..approvals import UNATTENDED_REFUSAL, Card, allowed_without_card, ask
from ..errors import ToolError, UpstreamError
from ..toolkit import Gate, ToolContext, ToolSpec, openai_schemas, split_result
from ..toolkit.registry import ALL_TOOLS
from .args import json_parser
from .types import Runner, ToolHandler, ToolOutcome, cap_tool_text

ROLES = frozenset({"supervisor", "member"})


def handlers(settings: Any, store: Any) -> list[ToolHandler]:
    """Every spec the settings allow, in pack order, offered per role and availability."""
    return [_handler(spec, settings, store) for spec in ALL_TOOLS]


def _handler(spec: ToolSpec, settings: Any, store: Any) -> ToolHandler:
    def offered(offer: Any) -> bool:
        """Settings and the live store both have a say: that is what ``usable`` is for."""
        return spec.available(settings) and spec.usable(offer, store)

    return ToolHandler(
        name=spec.name,
        label=spec.label,
        schema=openai_schemas([spec])[0],
        roles=spec.roles & ROLES,
        parse=json_parser(spec.label),
        run=_runner(spec),
        offered_when=offered,
        callable_when=offered,
    )


def _runner(spec: ToolSpec) -> Runner:
    async def run(ctx, payload: dict[str, Any]) -> ToolOutcome:
        client = ctx.http
        if client is None:  # pragma: no cover - startup always creates the client
            raise UpstreamError("服务未就绪，请稍后重试。")
        gate = spec.gate(ctx.settings, ctx.store, payload) if spec.gate is not None else None
        if gate is not None:
            refused = await _approve(ctx, spec, gate)
            if refused is not None:
                return refused
        context = ToolContext(
            store=ctx.store,
            settings=ctx.settings,
            run_id=ctx.run.run_id,
            conversation_id=ctx.conversation["id"],
            agent=ctx.author,
            http=client,
            publish=ctx.publish,
            run=ctx.run,
            message_id=ctx.source_message_id() if ctx.source_message_id else None,
            action_message_id=ctx.action_message_id,
            allow_outside=gate is not None,
        )
        value = await spec.run(context, payload)
        text, detail = split_result(value)
        summary = getattr(value, "summary", None) or spec.summary(payload)
        # A toolkit tool that produced pixels returns their attachment ids; the engine
        # appends them as a user turn after the batch (the tool result itself is text-only).
        images = [str(image) for image in getattr(value, "images", ()) or ()]
        return ToolOutcome(
            summary=summary,
            result={},
            text=cap_tool_text(text),
            detail=detail,
            images=images,
        )

    return run


async def _approve(ctx, spec: ToolSpec, gate: Gate) -> ToolOutcome | None:
    """Ask before a gated call, unless the bot's mode or a 「总是允许」 rule already says yes.

    ``None`` means go ahead; otherwise the finished outcome the model reads. A run nobody
    watches cannot answer a card, so there only the mode and the rules can say yes.
    """
    author = ctx.author
    card = Card(
        prompt=f"「{author['name']}」{gate.prompt}",
        confirm_label="允许",
        deny_label="拒绝",
        summary=f"「{author['name']}」{gate.summary}",
        step=f"等待你允许「{author['name']}」{spec.label}",
        resume_summary=gate.resume,
        resume_step=gate.resume,
        grant=gate.grant,
        rule=gate.rule,
    )
    if getattr(ctx.run, "schedule_id", None) is not None and not allowed_without_card(
        ctx, card, spec.name
    ):
        raise ToolError(UNATTENDED_REFUSAL)
    decision = await ask(ctx, card)
    if decision == "approve":
        return None
    denied = decision in ("deny", "stop")
    summary = f"你拒绝了：{gate.summary}" if denied else f"等待确认超时，没有执行：{gate.summary}"
    result = {"ok": False, "denied": True} if denied else {"ok": False, "error": summary}
    text = json.dumps(result, ensure_ascii=False)
    ctx.finish_action(
        ctx.action,
        status="completed" if denied else "error",
        summary=summary,
        error=None if denied else summary,
        result=text,
    )
    return ToolOutcome(summary=summary, result=result, text=text, handled=True)
