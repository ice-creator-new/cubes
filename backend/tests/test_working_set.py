"""The conversation's working set: detection, the store, and the hint a bot is handed.

The point of the feature is behavioural: a bot should never have to re-read a file a
teammate already wrote. These tests pin the three ways that could quietly fail — a touch
that is not detected, a path stored twice, and a stale or out-of-bounds file being inlined.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent import working_set
from helpers import store_of


def write_touch(path: Path, content: str) -> dict:
    """The ``detail`` a real ``file_write`` action row carries."""
    return {
        "kind": "diff",
        "path": str(path),
        "mode": "create",
        "added": len(content.splitlines()),
        "removed": 0,
    }


def test_a_write_is_recorded_once_and_the_newest_touch_wins(make_backend, tmp_path) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    conversation = store.direct_conversation()
    assert conversation is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    target = tmp_path / "ws" / "lib" / "app.dark"
    target.parent.mkdir(parents=True)
    target.write_text("one\ntwo\n", encoding="utf-8")

    working_set.record(
        store,
        backend.app.state.settings,
        conversation,
        supervisor,
        tool_name="file_write",
        arguments=json.dumps({"path": "lib/app.dark", "content": "one\ntwo\n"}),
        detail=write_touch(target, "one\ntwo\n"),
    )
    rows = store.list_working_files(conversation["id"])
    assert len(rows) == 1
    assert rows[0]["action"] == "write"
    assert rows[0]["lines"] == 2
    assert rows[0]["author_id"] == supervisor["id"]

    # A second touch of the same path updates that row instead of adding one.
    working_set.record(
        store,
        backend.app.state.settings,
        conversation,
        supervisor,
        tool_name="file_read",
        arguments=json.dumps({"path": "lib/app.dark"}),
    )
    rows = store.list_working_files(conversation["id"])
    assert len(rows) == 1
    assert rows[0]["action"] == "read"
    # The line count from the write survives a read, which knows nothing about the body.
    assert rows[0]["lines"] == 2


def test_the_hint_lists_the_newest_files_and_inlines_the_newest_small_ones(
    make_backend, tmp_path
) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    conversation = store.direct_conversation()
    assert conversation is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    settings = backend.app.state.settings
    folder = tmp_path / "ws"
    folder.mkdir(exist_ok=True)

    for name in ("old.dart", "new.dart"):
        (folder / name).write_text(f"// {name}\n", encoding="utf-8")
        working_set.record(
            store,
            settings,
            conversation,
            supervisor,
            tool_name="file_write",
            arguments=json.dumps({"path": name, "content": f"// {name}\n"}),
            detail=write_touch(folder / name, f"// {name}\n"),
        )
    hint = working_set.hint(store, settings, conversation)
    assert working_set.HINT_HEADER in hint
    # Newest first, and both bodies are shown, read from disk.
    assert hint.index("new.dart") < hint.index("old.dart")
    assert "<<<文件内容 new.dart" in hint
    assert "// old.dart" in hint


def test_a_deleted_or_changed_file_is_never_inlined_stale(make_backend, tmp_path) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    conversation = store.direct_conversation()
    assert conversation is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    settings = backend.app.state.settings
    folder = tmp_path / "ws"
    folder.mkdir(exist_ok=True)
    kept = folder / "kept.dart"
    gone = folder / "gone.dart"
    for path, body in ((kept, "// one\n"), (gone, "// two\n")):
        path.write_text(body, encoding="utf-8")
        working_set.record(
            store,
            settings,
            conversation,
            supervisor,
            tool_name="file_write",
            arguments=json.dumps({"path": path.name, "content": body}),
            detail=write_touch(path, body),
        )

    gone.unlink()
    kept.write_text("// one\n// changed since\n", encoding="utf-8")
    hint = working_set.hint(store, settings, conversation)
    assert "gone.dart" in hint and "已删除" in hint
    assert "kept.dart" in hint and "之后又被改动过" in hint
    # The changed file's new body is what gets shown: nothing stale is ever inlined.
    assert "// changed since" in hint
    assert "<<<文件内容 gone.dart" not in hint


def test_a_file_outside_the_allowed_folders_is_listed_but_never_inlined(
    make_backend, tmp_path
) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    conversation = store.direct_conversation()
    assert conversation is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    settings = backend.app.state.settings
    outside = tmp_path / "elsewhere" / "secret.dark"
    outside.parent.mkdir(parents=True)
    outside.write_text("SECRET BODY\n", encoding="utf-8")

    working_set.record(
        store,
        settings,
        conversation,
        supervisor,
        tool_name="file_write",
        arguments=json.dumps({"path": str(outside), "content": "SECRET BODY\n"}),
        detail={"path": str(outside), "mode": "create", "added": 1, "removed": 0},
    )
    hint = working_set.hint(store, settings, conversation)
    assert "secret.dark" in hint
    assert "SECRET BODY" not in hint
    assert "不在允许的文件夹内" in hint


def test_the_hint_lists_at_most_the_recent_files(make_backend, tmp_path) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    store = store_of(backend)
    conversation = store.direct_conversation()
    assert conversation is not None
    supervisor = store.supervisor()
    assert supervisor is not None
    settings = backend.app.state.settings
    folder = tmp_path / "ws"
    folder.mkdir(exist_ok=True)
    for number in range(working_set.HINT_FILES + 5):
        path = folder / f"file_{number}.txt"
        path.write_text("x\n", encoding="utf-8")
        store.touch_working_file(
            conversation["id"],
            path=str(path),
            author_id=supervisor["id"],
            author_name=supervisor["name"],
            action="read",
            size=2,
        )
    hint = working_set.hint(store, settings, conversation)
    listed = [line for line in hint.splitlines() if line.startswith("- ")]
    assert len(listed) == working_set.HINT_FILES
    # The newest touches are the ones listed; the oldest five are gone.
    assert "file_24.txt" in hint
    assert "file_0.txt" not in hint


def test_shell_reads_and_script_writes_are_detected(make_backend, tmp_path) -> None:
    backend = make_backend(workspace_dir=tmp_path / "ws")
    settings = backend.app.state.settings
    folder = tmp_path / "ws"
    folder.mkdir(exist_ok=True)
    read = folder / "lib" / "home.dart"
    read.parent.mkdir(parents=True)
    read.write_text("line\n", encoding="utf-8")
    other = folder / "lib" / "tokens.dart"
    other.write_text("Radius\n", encoding="utf-8")

    read_touch = working_set.touches(
        settings,
        tool_name="computer_shell",
        arguments=json.dumps({"command": "cd ~/proj && sed -n '1,80p' lib/home.dart"}),
    )
    # Relative to the ``cd`` target, which is not the working folder: nothing is guessed.
    assert read_touch == []

    found = working_set.touches(
        settings,
        tool_name="computer_shell",
        arguments=json.dumps({"command": f"cd {folder} && sed -n '1,80p' lib/home.dart"}),
    )
    assert [(Path(touch.path).name, touch.action) for touch in found] == [("home.dart", "read")]

    edited = working_set.touches(
        settings,
        tool_name="computer_shell",
        arguments=json.dumps(
            {
                "command": (
                    f"cd {folder} && python3 - <<'EOF'\n"
                    "import pathlib\n"
                    "p = pathlib.Path('lib/tokens.dart')\n"
                    "p.write_text('AppRadius')\n"
                    "EOF"
                )
            }
        ),
    )
    assert [(Path(touch.path).name, touch.action) for touch in edited] == [("tokens.dart", "write")]

    redirected = working_set.touches(
        settings,
        tool_name="computer_shell",
        arguments=json.dumps({"command": f"cd {folder} && echo hi > lib/home.dart"}),
    )
    assert [(Path(touch.path).name, touch.action) for touch in redirected] == [("home.dart", "write")]
