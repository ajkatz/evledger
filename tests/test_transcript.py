"""Unit tests for :mod:`evledger.transcript`.

Covers the window-scoped session-transcript loader, the turn normalizer, and
the budget chunker — over both a *captured* fixture transcript (the
``audit-transcript-loader`` task's required real-shape fixture) and synthetic
transcripts written into ``tmp_path`` for window / multi-session / tolerance
cases. No model is involved; the whole layer is pure stdlib.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evledger import (
    LoadResult,
    TranscriptChunk,
    TranscriptTurn,
    chunk_turns,
    default_projects_root,
    load_transcripts,
)

#: The captured, real-shape fixture transcript shipped with this task.
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "transcripts"


def _write_session(root: Path, session_id: str, records: list[dict]) -> Path:
    """Write a one-session JSONL transcript under ``root`` and return its path."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def _turn(
    *,
    type: str = "user",
    role: str = "user",
    content: object = "hello",
    session_id: str = "s1",
    uuid: str = "u1",
    timestamp: str | None = "2026-05-30T10:00:00Z",
) -> dict:
    """Build one transcript record dict in the Claude Code on-disk shape."""
    record: dict = {
        "type": type,
        "uuid": uuid,
        "sessionId": session_id,
        "message": {"role": role, "content": content},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


# --- captured fixture: the real on-disk shape -----------------------------


def test_loads_the_captured_fixture_transcript_turns() -> None:
    result = load_transcripts(root=FIXTURE_ROOT)

    # The fixture has 4 conversational turns worth keeping: two user
    # (the request + the thanks) and two assistant. The tool_result-only
    # user record, the summary, the file-history-snapshot, the blank line,
    # and the not-JSON line are all dropped.
    assert isinstance(result, LoadResult)
    assert result.files_read == 1
    assert [t.role for t in result.turns] == [
        "user",
        "assistant",
        "assistant",
        "user",
    ]


def test_fixture_assistant_tool_use_is_summarized_not_inlined() -> None:
    result = load_transcripts(root=FIXTURE_ROOT)

    first_assistant = result.turns[1]
    assert first_assistant.role == "assistant"
    # The text block survives; the tool_use collapses to a marker, not the
    # full input payload.
    assert "I'll start by reading the existing loader." in first_assistant.text
    assert "[tool_use: Read]" in first_assistant.text
    assert "/x/loader.py" not in first_assistant.text


def test_fixture_thinking_block_is_not_leaked_verbatim() -> None:
    result = load_transcripts(root=FIXTURE_ROOT)

    second_assistant = result.turns[2]
    assert "private chain of thought" not in second_assistant.text
    assert "[thinking]" in second_assistant.text
    assert "Done. I made the loader skip malformed lines." in second_assistant.text


def test_fixture_tool_result_only_user_record_is_dropped() -> None:
    result = load_transcripts(root=FIXTURE_ROOT)

    # No kept turn should carry the dropped tool-result blob.
    assert all("big file contents" not in t.text for t in result.turns)
    # The not-JSON line + the tool_result-only user record are both skipped.
    assert result.skipped_records >= 2


def test_fixture_provenance_session_id_and_uuid_present() -> None:
    result = load_transcripts(root=FIXTURE_ROOT)

    assert all(t.session_id == "session-abc123" for t in result.turns)
    assert result.turns[0].uuid == "u1"
    assert result.turns[0].time == "2026-05-30T10:00:00Z"


# --- window filtering -----------------------------------------------------


def test_since_is_inclusive_lower_bound(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", timestamp="2026-05-30T09:00:00Z", content="early"),
            _turn(uuid="b", timestamp="2026-05-30T10:00:00Z", content="boundary"),
            _turn(uuid="c", timestamp="2026-05-30T11:00:00Z", content="late"),
        ],
    )

    result = load_transcripts(root=tmp_path, since="2026-05-30T10:00:00Z")

    texts = [t.text for t in result.turns]
    assert texts == ["boundary", "late"]


def test_until_is_exclusive_upper_bound(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", timestamp="2026-05-30T09:00:00Z", content="early"),
            _turn(uuid="b", timestamp="2026-05-30T10:00:00Z", content="boundary"),
            _turn(uuid="c", timestamp="2026-05-30T11:00:00Z", content="late"),
        ],
    )

    result = load_transcripts(root=tmp_path, until="2026-05-30T10:00:00Z")

    texts = [t.text for t in result.turns]
    assert texts == ["early"]


def test_timeless_turns_survive_window_filtering(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", timestamp=None, content="no-timestamp"),
            _turn(uuid="b", timestamp="2026-05-30T11:00:00Z", content="late"),
        ],
    )

    # A tight window that excludes the timestamped turn must still keep the
    # timeless one — we can't place it on the timeline, so we never drop it.
    result = load_transcripts(
        root=tmp_path,
        since="2026-05-30T20:00:00Z",
        until="2026-05-30T21:00:00Z",
    )

    assert [t.text for t in result.turns] == ["no-timestamp"]


def test_accepts_aware_datetime_bounds(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [_turn(uuid="a", timestamp="2026-05-30T10:00:00Z", content="x")],
    )

    since = datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)
    result = load_transcripts(root=tmp_path, since=since)

    assert len(result.turns) == 1


def test_naive_datetime_bound_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_transcripts(root=tmp_path, since=datetime(2026, 5, 30, 9, 0))


# --- session selection + ordering -----------------------------------------


def test_session_id_selects_a_single_transcript(tmp_path: Path) -> None:
    _write_session(tmp_path, "s1", [_turn(session_id="s1", content="from-s1")])
    _write_session(tmp_path, "s2", [_turn(session_id="s2", content="from-s2")])

    result = load_transcripts(root=tmp_path, session_id="s2")

    assert result.files_read == 1
    assert [t.text for t in result.turns] == ["from-s2"]


def test_turns_are_chronological_across_sessions(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [_turn(session_id="s1", content="s1-late", timestamp="2026-05-30T12:00:00Z")],
    )
    _write_session(
        tmp_path,
        "s2",
        [_turn(session_id="s2", content="s2-early", timestamp="2026-05-30T08:00:00Z")],
    )

    result = load_transcripts(root=tmp_path)

    assert [t.text for t in result.turns] == ["s2-early", "s1-late"]


def test_mixed_subsecond_precision_sorts_chronologically(tmp_path: Path) -> None:
    # Lexical string order would put "...00Z" after "...00.5Z" (wrong); parsed
    # instants order them correctly.
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", content="half", timestamp="2026-05-30T10:00:00.500Z"),
            _turn(uuid="b", content="whole", timestamp="2026-05-30T10:00:00Z"),
        ],
    )

    result = load_transcripts(root=tmp_path)

    assert [t.text for t in result.turns] == ["whole", "half"]


def test_timeless_turn_sorts_before_timestamped(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", content="timed", timestamp="2026-05-30T10:00:00Z"),
            _turn(uuid="b", content="timeless", timestamp=None),
        ],
    )

    result = load_transcripts(root=tmp_path)

    assert [t.text for t in result.turns] == ["timeless", "timed"]


# --- tolerance ------------------------------------------------------------


def test_missing_root_returns_empty_result(tmp_path: Path) -> None:
    result = load_transcripts(root=tmp_path / "does-not-exist")

    assert result.turns == []
    assert result.files_read == 0


def test_string_content_message_is_normalized(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [_turn(content="just a plain string body")],
    )

    result = load_transcripts(root=tmp_path)

    assert result.turns[0].text == "just a plain string body"


def test_empty_text_turns_are_dropped(tmp_path: Path) -> None:
    # A turn whose content flattens to whitespace carries no signal.
    _write_session(
        tmp_path,
        "s1",
        [
            _turn(uuid="a", content="   "),
            _turn(uuid="b", content="real"),
        ],
    )

    result = load_transcripts(root=tmp_path)

    assert [t.text for t in result.turns] == ["real"]


def test_unparseable_timestamp_becomes_timeless_not_error(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "s1",
        [_turn(content="x", timestamp="not-a-timestamp")],
    )

    result = load_transcripts(root=tmp_path)

    assert len(result.turns) == 1
    assert result.turns[0].time is None


def test_only_jsonl_files_are_read(tmp_path: Path) -> None:
    _write_session(tmp_path, "s1", [_turn(content="kept")])
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")

    result = load_transcripts(root=tmp_path)

    assert result.files_read == 1
    assert [t.text for t in result.turns] == ["kept"]


# --- chunking -------------------------------------------------------------


def _turns(*lengths: int) -> list[TranscriptTurn]:
    """Build turns of the given character lengths (deterministic identities)."""
    return [
        TranscriptTurn(session_id="s", role="user", text="x" * n, uuid=f"u{i}")
        for i, n in enumerate(lengths)
    ]


def test_chunk_packs_consecutive_turns_under_budget() -> None:
    chunks = chunk_turns(_turns(40, 40, 40), max_chars=100)

    # 40 + 40 = 80 fits; adding the third (120) exceeds, so it starts a new chunk.
    assert [c.char_len for c in chunks] == [80, 40]
    assert [c.index for c in chunks] == [0, 1]
    assert sum(len(c.turns) for c in chunks) == 3


def test_chunk_emits_oversized_turn_as_its_own_chunk() -> None:
    chunks = chunk_turns(_turns(10, 500, 10), max_chars=100)

    assert [c.char_len for c in chunks] == [10, 500, 10]
    assert all(isinstance(c, TranscriptChunk) for c in chunks)


def test_chunk_empty_input_yields_no_chunks() -> None:
    assert chunk_turns([], max_chars=100) == []


def test_chunk_token_budget_converts_to_chars() -> None:
    # max_tokens=10 -> 40 chars (CHARS_PER_TOKEN=4); one 40-char turn fills it,
    # the next starts a new chunk.
    chunks = chunk_turns(_turns(40, 40), max_tokens=10)

    assert [c.char_len for c in chunks] == [40, 40]


def test_chunk_requires_exactly_one_budget() -> None:
    with pytest.raises(ValueError):
        chunk_turns(_turns(10), max_chars=10, max_tokens=10)
    with pytest.raises(ValueError):
        chunk_turns(_turns(10))


def test_chunk_rejects_nonpositive_budget() -> None:
    with pytest.raises(ValueError):
        chunk_turns(_turns(10), max_chars=0)


# --- default root ---------------------------------------------------------


def test_default_root_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))

    assert default_projects_root() == tmp_path


def test_default_root_falls_back_to_home_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)

    root = default_projects_root()

    assert root.parts[-2:] == (".claude", "projects")
