"""`@名字` parsing: the one rule routing and persistence both rely on."""

from __future__ import annotations

from app.mentions import parse_mentions

ROSTER = [
    {"id": "a_proto", "name": "原型设计师"},
    {"id": "a_proto_short", "name": "原型"},
    {"id": "a_jia", "name": "甲"},
    {"id": "a_yi", "name": "乙"},
]


def test_plain_mention_returns_the_agent_id() -> None:
    assert parse_mentions("@甲 帮我看一下", ROSTER) == ["a_jia"]
    assert parse_mentions("请交给 @乙", ROSTER) == ["a_yi"]


def test_overlapping_names_prefer_the_longest_match() -> None:
    assert parse_mentions("@原型设计师 先出图", ROSTER) == ["a_proto"]
    assert parse_mentions("@原型 先出图", ROSTER) == ["a_proto_short"]
    assert parse_mentions("@原型设计师和@原型 都看一下", ROSTER) == ["a_proto", "a_proto_short"]


def test_order_of_appearance_and_deduplication() -> None:
    text = "@乙先说，然后 @甲，最后 @乙 补充"
    assert parse_mentions(text, ROSTER) == ["a_yi", "a_jia"]
    assert parse_mentions("@甲@甲@甲", ROSTER) == ["a_jia"]


def test_full_width_marker_is_accepted() -> None:
    assert parse_mentions("＠甲 请看", ROSTER) == ["a_jia"]
    assert parse_mentions("@甲、＠乙：开始", ROSTER) == ["a_jia", "a_yi"]


def test_punctuation_and_suffixes_after_the_name_are_not_part_of_it() -> None:
    assert parse_mentions("@甲，请先出结论。", ROSTER) == ["a_jia"]
    assert parse_mentions("@甲的回答很有用", ROSTER) == ["a_jia"]
    assert parse_mentions("（@乙）", ROSTER) == ["a_yi"]


def test_unknown_names_are_ignored() -> None:
    assert parse_mentions("@丙 你好", ROSTER) == []
    assert parse_mentions("@甲 和 @丙 一起看", ROSTER) == ["a_jia"]
    assert parse_mentions("没有点名的消息", ROSTER) == []


def test_email_like_text_only_matches_a_real_name_after_the_marker() -> None:
    assert parse_mentions("发到 someone@example.com 就行", ROSTER) == []
    # The text before the marker is never a mention, even when a member owns that name.
    assert parse_mentions("甲@乙", ROSTER) == ["a_yi"]
    assert parse_mentions("a@b", [{"id": "a_b", "name": "b"}]) == ["a_b"]


def test_empty_inputs_are_harmless() -> None:
    assert parse_mentions("", ROSTER) == []
    assert parse_mentions("@甲", []) == []
    assert parse_mentions("@", ROSTER) == []
    assert parse_mentions("@甲", [{"id": "", "name": "甲"}]) == []
