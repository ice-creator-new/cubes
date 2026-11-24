"""Utility pack: clock, whitelisted calculator and unit conversion."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.toolkit import split_result, tool_specs
from app.toolkit.base import ToolContext, ToolInputError

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        store=None,  # type: ignore[arg-type]
        settings=Settings(database_path=tmp_path / "cubes.sqlite3"),
        run_id="r1",
        conversation_id="c1",
        agent={"id": "a1", "name": "主管", "kind": "supervisor"},
        http=httpx.AsyncClient(trust_env=False),
        publish=lambda event, payload: None,
    )


def spec(name: str, tmp_path: Path):
    settings = Settings(database_path=tmp_path / "cubes.sqlite3")
    return next(s for s in tool_specs(settings, "supervisor") if s.name == name)


async def call(tmp_path: Path, name: str, args: dict) -> str:
    ctx = context(tmp_path)
    try:
        text, _ = split_result(await spec(name, tmp_path).run(ctx, args))
        return text
    finally:
        await ctx.http.aclose()


# -------------------------------------------------------------------- time


def test_current_time_defaults_to_shanghai(tmp_path: Path) -> None:
    async def body() -> None:
        text = await call(tmp_path, "current_time", {})
        first = text.splitlines()[0]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+08:00 星期[一二三四五六日]", first)
        assert "时区：Asia/Shanghai" in text
        expected = WEEKDAYS[datetime.now(ZoneInfo("Asia/Shanghai")).weekday()]
        assert first.endswith(expected)
        assert "UTC：" in text

    run(body())


def test_current_time_honours_the_timezone(tmp_path: Path) -> None:
    async def body() -> None:
        text = await call(tmp_path, "current_time", {"timezone": "UTC"})
        assert "+00:00" in text
        assert "时区：UTC" in text
        local = await call(tmp_path, "current_time", {"timezone": "America/New_York"})
        assert "时区：America/New_York" in local
        for bad in ("Mars/Olympus", "../../etc/passwd"):
            with pytest.raises(ToolInputError) as caught:
                await call(tmp_path, "current_time", {"timezone": bad})
            assert "时区" in str(caught.value)
        default = await call(tmp_path, "current_time", {"timezone": ""})
        assert "时区：Asia/Shanghai" in default

    run(body())


# --------------------------------------------------------------- calculate


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", "7"),
        ("(1 + 2) * 3", "9"),
        ("2 ** 10", "1024"),
        ("7 // 2", "3"),
        ("7 % 3", "1"),
        ("-3 + 1", "-2"),
        ("sqrt(16)", "4"),
        ("round(3.14159, 2)", "3.14"),
        ("max(1, 5, 3)", "5"),
        ("min(4, 2)", "2"),
        ("abs(-3)", "3"),
        ("floor(2.9)", "2"),
        ("ceil(2.1)", "3"),
        ("log(e)", "1"),
        ("sin(0)", "0"),
    ],
)
def test_calculate_evaluates_whitelisted_math(tmp_path: Path, expression: str, expected: str) -> None:
    async def body() -> None:
        text = await call(tmp_path, "calculate", {"expression": expression})
        assert text == f"{expression} = {expected}"

    run(body())


def test_calculate_handles_fractions_and_constants(tmp_path: Path) -> None:
    async def body() -> None:
        third = await call(tmp_path, "calculate", {"expression": "1 / 3"})
        assert third.startswith("1 / 3 = 0.333333333333")
        tau = await call(tmp_path, "calculate", {"expression": "2 * pi"})
        assert tau.startswith("2 * pi = 6.28318530718")

    run(body())


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "1 +",
        "__import__('os').system('ls')",
        "().__class__",
        "[1, 2]",
        "x + 1",
        "'a' + 'b'",
        "1 if 1 else 2",
        "lambda: 1",
        "9 ** 9 ** 9",
        "2 ** 100000",
        "1 / 0",
        "sqrt(-1)",
        "open('/etc/passwd')",
        "{'a': 1}",
    ],
)
def test_calculate_rejects_anything_else(tmp_path: Path, expression: str) -> None:
    async def body() -> None:
        with pytest.raises(ToolInputError) as caught:
            await call(tmp_path, "calculate", {"expression": expression})
        assert str(caught.value).endswith("。")

    run(body())


# ------------------------------------------------------------- convert_units


@pytest.mark.parametrize(
    ("value", "source", "target", "prefix"),
    [
        (1, "km", "m", "1 km = 1000 m"),
        (1000, "米", "公里", "1000 米 = 1 公里"),
        (1, "mile", "km", "1 mile = 1.609344 km"),
        (1, "kg", "g", "1 kg = 1000 g"),
        (1, "lb", "g", "1 lb = 453.59237 g"),
        (1, "h", "min", "1 h = 60 min"),
        (1.5, "小时", "分钟", "1.5 小时 = 90 分钟"),
        (1, "GB", "MB", "1 GB = 1000 MB"),
        (1, "GiB", "MiB", "1 GiB = 1024 MiB"),
        (1, "GB", "MiB", "1 GB = 953.674316 MiB"),
        (60, "km/h", "m/s", "60 km/h = 16.666667 m/s"),
        (1, "acre", "m2", "1 acre = 4046.856422 m2"),
        (1, "公顷", "平方米", "1 公顷 = 10000 平方米"),
    ],
)
def test_convert_units_covers_every_family(
    tmp_path: Path, value: float, source: str, target: str, prefix: str
) -> None:
    async def body() -> None:
        text = await call(tmp_path, "convert_units", {"value": value, "from": source, "to": target})
        assert text.startswith(prefix)

    run(body())


def test_convert_temperature_is_affine(tmp_path: Path) -> None:
    async def body() -> None:
        freezing = await call(tmp_path, "convert_units", {"value": 0, "from": "C", "to": "F"})
        assert freezing.startswith("0 C = 32 F")
        boiling = await call(tmp_path, "convert_units", {"value": 100, "from": "F", "to": "C"})
        assert re.match(r"100 F = 37\.7777", boiling)
        kelvin = await call(tmp_path, "convert_units", {"value": 0, "from": "C", "to": "K"})
        assert kelvin.startswith("0 C = 273.15 K")
        body_temp = await call(tmp_path, "convert_units", {"value": 310.15, "from": "K", "to": "°C"})
        assert body_temp.startswith("310.15 K = 37 C")

    run(body())


def test_convert_units_rejects_unknown_and_mixed_units(tmp_path: Path) -> None:
    async def body() -> None:
        cases = [
            {"value": 1, "from": "m", "to": "kg"},
            {"value": 1, "from": "parsec", "to": "m"},
            {"value": 1, "from": "m", "to": "parsec"},
        ]
        for args in cases:
            with pytest.raises(ToolInputError) as caught:
                await call(tmp_path, "convert_units", args)
            assert str(caught.value).endswith("。")

    run(body())
