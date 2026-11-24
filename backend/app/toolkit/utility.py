"""Pack C — utility: clock, a whitelisted calculator and unit conversion."""

from __future__ import annotations

import ast
import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import ToolContext, ToolInputError, ToolResult, ToolSpec, text_detail

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
DEFAULT_TIMEZONE = "Asia/Shanghai"
MAX_EXPRESSION_CHARS = 400
MAX_NODES = 120
MAX_EXPONENT = 1_000
MAX_MAGNITUDE = 1e300

CURRENT_TIME_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "timezone": {
            "type": "string",
            "description": f"IANA 时区名，默认 {DEFAULT_TIMEZONE}，例如 America/New_York。",
        },
    },
}

CALCULATE_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "expression": {
            "type": "string",
            "description": "算式，支持 + - * / // % **、括号和 sqrt log exp sin cos tan floor ceil abs round min max pi e。",
        },
    },
    "required": ["expression"],
}

CONVERT_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "value": {"type": "number", "description": "要换算的数值。"},
        "from": {"type": "string", "description": "原单位，中英文均可，例如 km、公里、°C。"},
        "to": {"type": "string", "description": "目标单位，与 from 属于同一类。"},
    },
    "required": ["value", "from", "to"],
}


# -------------------------------------------------------------------- clock


async def run_current_time(ctx: ToolContext, args: dict) -> str:
    raw = args.get("timezone")
    if raw in (None, ""):
        name = DEFAULT_TIMEZONE
    elif not isinstance(raw, str) or len(raw.strip()) > 64:
        raise ToolInputError("时区名无效。")
    else:
        name = raw.strip()
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ToolInputError(f"时区「{name}」不受支持。") from exc
    now = datetime.now(zone)
    iso = now.isoformat(timespec="seconds")
    match = re.fullmatch(r"(.+?)T(\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2}|Z)", iso)
    if match:
        date_part, time_part, offset = match.groups()
        offset = offset if offset != "Z" else "+00:00"
        if ":" not in offset:
            offset = f"{offset[:3]}:{offset[3:]}"
        head = f"{date_part} {time_part} {offset} {WEEKDAYS[now.weekday()]}"
    else:  # pragma: no cover - every real timezone carries an offset
        head = f"{now:%Y-%m-%d %H:%M:%S} {WEEKDAYS[now.weekday()]}"
    utc_now = datetime.now(timezone.utc)
    text = "\n".join([head, f"时区：{name}", f"UTC：{utc_now:%Y-%m-%d %H:%M:%S}"])
    return ToolResult(text, text_detail(text))


# ---------------------------------------------------------------- calculate

_BINARY_OPS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: left // right,
    ast.Mod: lambda left, right: left % right,
    ast.Pow: lambda left, right: left**right,
}
_FUNCTIONS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _guarded(expression: str) -> ast.Expression:
    if not expression.strip():
        raise ToolInputError("算式不能为空。")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ToolInputError(f"算式不能超过 {MAX_EXPRESSION_CHARS} 个字符。")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolInputError("算式写法有误，无法解析。") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ToolInputError("算式太复杂了。")
    return tree


def _evaluate(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolInputError("只能用数字和受支持的运算符。")
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolInputError("数字超出范围。")
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _evaluate(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
        operator = _BINARY_OPS.get(type(node.op))
        if operator is None:
            raise ToolInputError("这个运算符不受支持。")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, float) and right != int(right):
                raise ToolInputError("指数必须是整数。")
            if abs(float(right)) > MAX_EXPONENT:
                raise ToolInputError(f"指数不能超过 {MAX_EXPONENT}。")
            if abs(float(left)) > 1.0 and abs(float(right)) * math.log10(abs(float(left))) > 300:
                raise ToolInputError("结果太大，无法计算。")
        result = operator(left, right)
        if isinstance(result, complex):
            raise ToolInputError("结果不是实数，无法计算。")
        if isinstance(result, float) and not math.isfinite(result):
            raise ToolInputError("结果超出数值范围。")
        if abs(result) > MAX_MAGNITUDE:
            raise ToolInputError("结果太大，无法计算。")
        return result
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ToolInputError("只能调用受支持的数学函数。")
        function = _FUNCTIONS.get(node.func.id)
        if function is None:
            raise ToolInputError("只能调用受支持的数学函数。")
        return function(*[_evaluate(argument) for argument in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ToolInputError(f"不认识的名称「{node.id}」。")
    raise ToolInputError("只能用数字、括号和受支持的运算符。")


def _format_number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ToolInputError("结果超出数值范围。")
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return format(value, ".12g")


async def run_calculate(ctx: ToolContext, args: dict) -> str:
    raw = args.get("expression")
    if not isinstance(raw, str):
        raise ToolInputError("算式不能为空。")
    expression = raw.strip()
    tree = _guarded(expression)
    try:
        value = _evaluate(tree)
    except ToolInputError:
        raise
    except ZeroDivisionError as exc:
        raise ToolInputError("不能除以零。") from exc
    except (ValueError, OverflowError) as exc:
        raise ToolInputError(f"计算出错：{type(exc).__name__}。") from exc
    text = f"{expression} = {_format_number(value)}"
    return ToolResult(text, text_detail(text))


# ------------------------------------------------------------- convert_units

_UNITS: dict[str, dict[str, tuple[str, float]]] = {
    "length": {
        "m": ("m", 1.0), "米": ("米", 1.0),
        "km": ("km", 1000.0), "千米": ("千米", 1000.0), "公里": ("公里", 1000.0),
        "cm": ("cm", 0.01), "厘米": ("厘米", 0.01),
        "mm": ("mm", 0.001), "毫米": ("毫米", 0.001),
        "mi": ("mi", 1609.344), "mile": ("mile", 1609.344), "miles": ("mile", 1609.344),
        "英里": ("英里", 1609.344),
        "ft": ("ft", 0.3048), "foot": ("ft", 0.3048), "feet": ("ft", 0.3048),
        "英尺": ("英尺", 0.3048),
        "in": ("in", 0.0254), "inch": ("in", 0.0254), "英寸": ("英寸", 0.0254),
        "yd": ("yd", 0.9144), "yard": ("yd", 0.9144), "码": ("码", 0.9144),
        "nmi": ("nmi", 1852.0), "海里": ("海里", 1852.0),
    },
    "mass": {
        "g": ("g", 1.0), "克": ("克", 1.0),
        "kg": ("kg", 1000.0), "千克": ("千克", 1000.0), "公斤": ("公斤", 1000.0),
        "mg": ("mg", 0.001), "毫克": ("毫克", 0.001),
        "t": ("t", 1e6), "ton": ("t", 1e6), "吨": ("吨", 1e6),
        "lb": ("lb", 453.59237), "lbs": ("lb", 453.59237), "pound": ("lb", 453.59237),
        "磅": ("磅", 453.59237),
        "oz": ("oz", 28.349523125), "ounce": ("oz", 28.349523125), "盎司": ("盎司", 28.349523125),
    },
    "time": {
        "s": ("s", 1.0), "sec": ("s", 1.0), "秒": ("秒", 1.0),
        "ms": ("ms", 0.001), "毫秒": ("毫秒", 0.001),
        "min": ("min", 60.0), "minute": ("min", 60.0), "分钟": ("分钟", 60.0),
        "h": ("h", 3600.0), "hour": ("h", 3600.0), "小时": ("小时", 3600.0),
        "d": ("d", 86400.0), "day": ("d", 86400.0), "天": ("天", 86400.0),
    },
    "data": {
        "b": ("B", 1.0), "byte": ("B", 1.0), "字节": ("B", 1.0),
        "kb": ("KB", 1e3), "mb": ("MB", 1e6), "gb": ("GB", 1e9), "tb": ("TB", 1e12),
        "kib": ("KiB", 1024.0), "mib": ("MiB", 1024.0**2), "gib": ("GiB", 1024.0**3),
        "tib": ("TiB", 1024.0**4),
    },
    "speed": {
        "m/s": ("m/s", 1.0), "米每秒": ("m/s", 1.0),
        "km/h": ("km/h", 1000.0 / 3600.0), "kmh": ("km/h", 1000.0 / 3600.0),
        "kph": ("km/h", 1000.0 / 3600.0), "公里每小时": ("km/h", 1000.0 / 3600.0),
        "千米每小时": ("km/h", 1000.0 / 3600.0),
        "mph": ("mph", 0.44704), "英里每小时": ("mph", 0.44704),
        "kn": ("kn", 1852.0 / 3600.0), "knot": ("kn", 1852.0 / 3600.0),
        "节": ("kn", 1852.0 / 3600.0),
    },
    "area": {
        "m2": ("m2", 1.0), "平方米": ("m2", 1.0),
        "km2": ("km2", 1e6), "平方公里": ("km2", 1e6), "平方千米": ("km2", 1e6),
        "cm2": ("cm2", 1e-4), "平方厘米": ("cm2", 1e-4),
        "ha": ("ha", 1e4), "hectare": ("ha", 1e4), "公顷": ("ha", 1e4),
        "acre": ("acre", 4046.8564224), "英亩": ("acre", 4046.8564224),
        "ft2": ("ft2", 0.09290304), "平方英尺": ("ft2", 0.09290304),
        "mi2": ("mi2", 2589988.110336), "平方英里": ("mi2", 2589988.110336),
    },
}
_TEMPERATURE_UNITS = {"c", "celsius", "摄氏度", "f", "fahrenheit", "华氏度", "k", "kelvin", "开尔文"}
CATEGORY_LABELS = {
    "length": "长度",
    "mass": "质量",
    "time": "时间",
    "data": "数据",
    "temperature": "温度",
    "speed": "速度",
    "area": "面积",
}


def _normalise_unit(name: str) -> str:
    text = str(name).strip().lower().replace("²", "2").replace("°", "")
    return text


def _display_unit(name: str) -> str:
    return str(name).strip().replace("°", "").replace("  ", " ")


def _lookup(unit: str) -> tuple[str, str, float]:
    """``(category, canonical, factor)`` for one unit name; temperature keeps factor 1."""
    key = _normalise_unit(unit)
    if key in _TEMPERATURE_UNITS:
        if key.startswith("f") or key in ("华氏度", "fahrenheit"):
            return "temperature", "F", 1.0
        if key.startswith("k") or key in ("kelvin", "开尔文"):
            return "temperature", "K", 1.0
        return "temperature", "C", 1.0
    for category, table in _UNITS.items():
        entry = table.get(key)
        if entry is not None:
            return category, entry[0], entry[1]
    raise ToolInputError(f"不认识的单位「{unit}」。")


def _to_celsius(value: float, canonical: str) -> float:
    if canonical == "C":
        return value
    if canonical == "F":
        return (value - 32.0) * 5.0 / 9.0
    return value - 273.15


def _from_celsius(value: float, canonical: str) -> float:
    if canonical == "C":
        return value
    if canonical == "F":
        return value * 9.0 / 5.0 + 32.0
    return value + 273.15


def _format_quantity(value: float) -> str:
    rounded = round(float(value), 6)
    if abs(rounded) < 1e-12:
        return "0"
    if abs(rounded - int(rounded)) < 1e-12 and abs(rounded) < 1e16:
        return str(int(rounded))
    return format(rounded, ".12g")


async def run_convert_units(ctx: ToolContext, args: dict) -> str:
    raw_value = args.get("value")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ToolInputError("数值必须是数字。")
    source = args.get("from")
    target = args.get("to")
    if not isinstance(source, str) or not source.strip():
        raise ToolInputError("原单位不能为空。")
    if not isinstance(target, str) or not target.strip():
        raise ToolInputError("目标单位不能为空。")
    source_category, source_canonical, source_factor = _lookup(source)
    target_category, target_canonical, target_factor = _lookup(target)
    if source_category != target_category:
        raise ToolInputError(
            f"不能把{CATEGORY_LABELS[source_category]}换算成{CATEGORY_LABELS[target_category]}。"
        )
    value = float(raw_value)
    if source_category == "temperature":
        result = _from_celsius(_to_celsius(value, source_canonical), target_canonical)
    else:
        result = value * source_factor / target_factor
    text = (
        f"{_format_quantity(value)} {_display_unit(source)} = "
        f"{_format_quantity(result)} {_display_unit(target)}"
    )
    return ToolResult(text, text_detail(text))


def _summary(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


UTILITY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="current_time",
        label="查看时间",
        description=(
            "获取当前日期和时间（含星期）。凡是要写「今天」「本周」或计算截止时间，先调用它，不要凭猜测。"
        ),
        parameters=CURRENT_TIME_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_current_time,
        summary=lambda args: "查看当前时间",
    ),
    ToolSpec(
        name="calculate",
        label="计算",
        description=(
            "精确计算算式，支持四则运算、取余、整除、乘方和常用数学函数。"
            "数字运算不要心算，用这个工具保证结果正确。"
        ),
        parameters=CALCULATE_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_calculate,
        summary=lambda args: f"计算 {_summary(args.get('expression'), 30)}",
    ),
    ToolSpec(
        name="convert_units",
        label="换算单位",
        description=(
            "长度、质量、时间、数据、温度、速度和面积的单位换算，中英文单位名都支持。"
        ),
        parameters=CONVERT_PARAMETERS,
        roles=frozenset({"supervisor", "member"}),
        run=run_convert_units,
        summary=lambda args: f"换算 {_summary(args.get('from'), 12)} 到 {_summary(args.get('to'), 12)}",
    ),
)
