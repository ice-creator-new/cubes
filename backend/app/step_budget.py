"""How many model and tool calls one turn and one run may spend.

There is no fixed ceiling by default: an agent task ends when the model stops asking for
tools, the way other agent products work. A workspace may still choose a finite preset in
「设置 → 团队」 for cost control; the standard preset keeps honouring the ``MAX_*``
environment variables, so an operator's own tuning still applies there.

A resolved number of ``0`` means "no limit". Context growth is handled where it always was:
a request is folded into a summary at the start of each bot turn and trimmed to fit when a
single turn runs long, so a task is never stopped just because it took many steps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# Presets in the order the settings page shows them: the default first, then finite ceilings.
PRESETS = ("unlimited", "standard", "long", "marathon")
# New and existing workspaces start unlimited: the old 32-tool ceiling stopped real work.
DEFAULT_PRESET = "unlimited"

# A workflow walks many bounded turns inside one run, so its cap is wider than a chat's. The
# ratios are the ones the standard preset already had (2.5x model calls, 2x tool calls), and
# the derived numbers never drop below the standard caps.
WORKFLOW_MODEL_RATIO = 2.5
WORKFLOW_TOOL_RATIO = 2.0

# The finite presets, as (turn model, turn tool, run model, run tool). Standard is resolved
# from ``Settings`` so the environment variables keep their say.
_PRESET_NUMBERS: dict[str, tuple[int, int, int, int]] = {
    "long": (64, 96, 192, 384),
    "marathon": (160, 256, 400, 800),
}

# The sandbox computer's own caps, as (total, shell, browser, screen). Standard is resolved
# from ``Settings`` (40/30/25/8 by default); the wider presets scale the same shape, so a long
# or marathon workspace is not stopped by the sandbox long before its call budget runs out.
_COMPUTER_NUMBERS: dict[str, tuple[int, int, int, int]] = {
    "long": (120, 90, 75, 24),
    "marathon": (300, 225, 190, 60),
}


@dataclass(frozen=True)
class StepBudget:
    """One workspace's resolved limits, in the names the engine checks them by.

    Every call number is 0 when there is no limit; the engine must skip the check rather
    than compare against it.
    """

    preset: str
    unlimited: bool
    turn_model_calls: int
    turn_tool_calls: int
    run_model_calls: int
    run_tool_calls: int
    workflow_model_calls: int
    workflow_tool_calls: int
    # The sandbox computer's budget: the total and each tool's own share.
    computer_steps: int
    computer_shell_steps: int
    computer_browser_steps: int
    computer_screen_steps: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unlimited() -> StepBudget:
    return StepBudget(
        preset="unlimited",
        unlimited=True,
        turn_model_calls=0,
        turn_tool_calls=0,
        run_model_calls=0,
        run_tool_calls=0,
        workflow_model_calls=0,
        workflow_tool_calls=0,
        computer_steps=0,
        computer_shell_steps=0,
        computer_browser_steps=0,
        computer_screen_steps=0,
    )


def _workflow_caps(settings, run_model_calls: int, run_tool_calls: int) -> tuple[int, int]:
    """Workflow caps for a scaled run: the standard ratio, never under the standard caps."""
    return (
        max(settings.max_workflow_model_calls, int(run_model_calls * WORKFLOW_MODEL_RATIO)),
        max(settings.max_workflow_tool_calls, int(run_tool_calls * WORKFLOW_TOOL_RATIO)),
    )


def preset_budget(preset: str, settings) -> StepBudget:
    """The resolved numbers of one preset; an unknown name reads as the standard preset."""
    if preset == "unlimited":
        return _unlimited()
    if preset == "standard" or preset not in _PRESET_NUMBERS:
        return StepBudget(
            preset="standard",
            unlimited=False,
            turn_model_calls=settings.max_turn_model_calls,
            turn_tool_calls=settings.max_turn_tool_calls,
            run_model_calls=settings.max_model_calls,
            run_tool_calls=settings.max_tool_calls,
            workflow_model_calls=settings.max_workflow_model_calls,
            workflow_tool_calls=settings.max_workflow_tool_calls,
            computer_steps=settings.max_computer_steps,
            computer_shell_steps=settings.max_computer_shell_steps,
            computer_browser_steps=settings.max_computer_browser_steps,
            computer_screen_steps=settings.max_computer_screen_steps,
        )
    turn_model, turn_tool, run_model, run_tool = _PRESET_NUMBERS[preset]
    workflow_model, workflow_tool = _workflow_caps(settings, run_model, run_tool)
    computer_total, computer_shell, computer_browser, computer_screen = _COMPUTER_NUMBERS[preset]
    return StepBudget(
        preset=preset,
        unlimited=False,
        turn_model_calls=turn_model,
        turn_tool_calls=turn_tool,
        run_model_calls=run_model,
        run_tool_calls=run_tool,
        workflow_model_calls=workflow_model,
        workflow_tool_calls=workflow_tool,
        computer_steps=computer_total,
        computer_shell_steps=computer_shell,
        computer_browser_steps=computer_browser,
        computer_screen_steps=computer_screen,
    )


def all_presets(settings) -> dict[str, dict[str, Any]]:
    """Every preset's resolved numbers, so the settings page can label all of them."""
    return {preset: preset_budget(preset, settings).as_dict() for preset in PRESETS}


def budget_for(store, settings) -> StepBudget:
    """The workspace's effective budget right now; one read per check, no cached copy."""
    return preset_budget(store.step_budget(), settings)
