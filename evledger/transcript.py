"""Window-scoped Claude Code session transcript loader + chunker.

This module is the **transcript-loader layer** of the model-powered oversight
audit (the ``audit-transcript-loader`` task of the
``oversight-analyzer-model-powered-audit-necessity`` whim). It is the first of
three layers; it is deliberately the *deterministic, model-free* one so the
plumbing under the audit is unit-testable without a live model.

Its job is narrow and pure:

1. **Locate** the on-disk Claude Code session transcript files for a time
   window (and, optionally, a single session id).
2. **Read + normalize** each transcript's JSONL records into a minimal,
   stable :class:`TranscriptTurn` list — just the fields the audit model needs
   (role, text, timestamp, session id), dropping the large tool-result blobs,
   thinking traces, and bookkeeping records that would only burn the model's
   context budget.
3. **Chunk** the normalized turns into model-budget-sized batches so the
   reconstruction layer (a later task) can feed them to the model without
   blowing a context window.

On-disk layout (Claude Code convention)
---------------------------------------
Claude Code stores one JSONL file per session under a per-project directory::

    ~/.claude/projects/<encoded-project-path>/<session-id>.jsonl

Each line is one JSON record. The records this loader cares about are the
``user`` and ``assistant`` turns; everything else (``summary`` markers,
``file-history-snapshot`` records, queued-command stubs, …) is skipped. The
loader is **tolerant**: a missing root, a non-``.jsonl`` file, a blank line, a
record that is not valid JSON, or a record missing the fields we need are all
*dropped and counted* rather than raised — mirroring
:class:`~evledger.store.ReadResult`.

Standalone + path-overridable
-----------------------------
Like the rest of the ledger this module imports only the standard library and
the sibling :mod:`~evledger.schema` layer (for the shared UTC time
helpers). The Claude Code ``~/.claude/projects`` location is the audit
feature's natural home — but it is only the *default*: :func:`load_transcripts`
takes an explicit ``root`` so tests (and any adopter) point it at their own
directory, keeping the path out of the hard-coded core.

The output is data, not side effects: nothing is written, every input file is
read once, and the produced :class:`TranscriptTurn` values are frozen.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from evledger.schema import parse_time

#: Suffix identifying a session transcript file.
JSONL_SUFFIX = ".jsonl"

#: Record ``type`` values that carry a conversational turn we keep.
_TURN_TYPES = ("user", "assistant")

#: Rough characters-per-token heuristic for the default chunk budget. Real
#: tokenization is model-specific; the loader stays model-free and only needs
#: an order-of-magnitude bound to split work, so a conservative 4 chars/token
#: (typical for English prose + code) is plenty. Callers that know their
#: model's tokenizer can pass an exact ``max_chars`` instead.
CHARS_PER_TOKEN = 4


def default_projects_root() -> Path:
    """Return the default Claude Code session-transcript root.

    Honors ``$CLAUDE_PROJECTS_DIR`` when set (so a headless/cron context can
    point the audit at a non-default location), otherwise falls back to the
    conventional ``~/.claude/projects``. The path is **not** required to
    exist — :func:`load_transcripts` tolerates a missing root.
    """
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class TranscriptTurn:
    """One normalized conversational turn from a session transcript.

    The minimal projection the audit model needs — the heavy tool-result
    payloads, thinking blocks, and bookkeeping records are dropped upstream in
    :func:`load_transcripts`, so a :class:`TranscriptTurn` is cheap to hold and
    cheap to serialize into a model prompt.

    Attributes:
        session_id: The session this turn belongs to (the transcript file's
            stem, or the record's own ``sessionId`` when present). Provenance
            for the reconstructed events the audit will emit.
        role: ``"user"`` or ``"assistant"``.
        text: The turn's flattened text content. Multi-block messages (a list
            of content blocks) are joined; non-text blocks are summarized to a
            compact placeholder (e.g. ``"[tool_use: Bash]"``) so the model sees
            that a tool ran without the full payload.
        time: The record's ISO-8601 UTC timestamp (``Z``-suffixed), or ``None``
            when the record carried no parseable timestamp.
        uuid: The record's own ``uuid`` when present — a stable identity for
            dedup / signature use by the reconstruction layer. ``None`` when
            absent.
    """

    session_id: str
    role: str
    text: str
    time: str | None = None
    uuid: str | None = None

    @property
    def char_len(self) -> int:
        """Character length of the turn's text (the chunker's cost unit)."""
        return len(self.text)


@dataclass(frozen=True)
class LoadResult:
    """The outcome of loading transcripts for a window.

    Attributes:
        turns: The normalized, kept turns in chronological order (by parsed
            timestamp; turns with no timestamp sort first, ties broken by
            ``(session_id, file order)``).
        files_read: How many transcript files were opened and parsed.
        skipped_records: Non-blank records that were dropped — not valid JSON,
            not a kept turn type, or missing the fields we need.
        malformed_files: Files that could not be read at all (OS error).
    """

    turns: list[TranscriptTurn] = field(default_factory=list)
    files_read: int = 0
    skipped_records: int = 0
    malformed_files: int = 0


@dataclass(frozen=True)
class TranscriptChunk:
    """A model-budget-sized batch of consecutive turns.

    Attributes:
        turns: The turns in this chunk, in the same order as the input.
        char_len: The summed character length of the chunk's turns.
        index: The chunk's 0-based position in the produced sequence.
    """

    turns: Sequence[TranscriptTurn]
    char_len: int
    index: int


# --- text flattening ------------------------------------------------------


def _summarize_block(block: dict[str, object]) -> str:
    """Render a single content block to a compact placeholder (or drop it).

    * ``text`` → the text itself.
    * ``tool_use`` → a ``[tool_use: <name>]`` marker so the model sees *that*
      a tool ran (and which) without ingesting the full input payload.
    * ``thinking`` → a ``[thinking]`` marker; the private chain-of-thought is
      never inlined.
    * ``tool_result`` → dropped (empty). A tool result is the *environment's*
      output, not a conversational turn; a user record consisting solely of
      tool results carries no audit signal and is filtered out entirely (it
      flattens to empty and the turn is dropped upstream).
    * any other named block → a ``[<kind>]`` marker.
    """
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        return text if isinstance(text, str) else ""
    if kind == "tool_use":
        name = block.get("name")
        return f"[tool_use: {name}]" if isinstance(name, str) else "[tool_use]"
    if kind == "tool_result":
        return ""
    if kind == "thinking":
        return "[thinking]"
    if isinstance(kind, str):
        return f"[{kind}]"
    return ""


def _flatten_content(content: object) -> str:
    """Flatten a message ``content`` (string or block list) into one string.

    A plain string is returned as-is. A list of content blocks has each block
    summarized via :func:`_summarize_block` and the non-empty pieces joined
    with newlines. Anything else flattens to an empty string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = [
            _summarize_block(b)
            for b in content
            if isinstance(b, dict)
        ]
        return "\n".join(p for p in pieces if p)
    return ""


def _normalize_record(record: object, fallback_session_id: str) -> TranscriptTurn | None:
    """Turn one parsed JSONL record into a :class:`TranscriptTurn`, or ``None``.

    Returns ``None`` (so the caller counts it as skipped) when the record is
    not a dict, is not a kept turn type, has no ``message``, or flattens to an
    empty text body. The ``message`` may be either the Anthropic message object
    (``{"role", "content"}``) or, defensively, a bare string.
    """
    if not isinstance(record, dict):
        return None
    if record.get("type") not in _TURN_TYPES:
        return None

    message = record.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        content = message.get("content")
    elif isinstance(message, str):
        role = record.get("type")
        content = message
    else:
        return None

    if not isinstance(role, str):
        role = record.get("type")
        if not isinstance(role, str):
            return None

    text = _flatten_content(content).strip()
    if not text:
        return None

    time_value = record.get("timestamp")
    time_str = _normalize_time(time_value) if isinstance(time_value, str) else None

    session_value = record.get("sessionId")
    session_id = session_value if isinstance(session_value, str) and session_value else fallback_session_id

    uuid_value = record.get("uuid")
    uuid_str = uuid_value if isinstance(uuid_value, str) else None

    return TranscriptTurn(
        session_id=session_id,
        role=role,
        text=text,
        time=time_str,
        uuid=uuid_str,
    )


def _normalize_time(text: str) -> str | None:
    """Normalize a timestamp string to canonical ``Z``-suffixed UTC, or ``None``.

    Delegates to :func:`~evledger.schema.parse_time` (which handles the
    ``Z`` suffix and numeric offsets) and re-renders. A value we cannot parse
    is treated as no-timestamp rather than raised, keeping the loader tolerant.
    """
    try:
        instant = parse_time(text)
    except ValueError:
        return None
    rendered = instant.isoformat()
    if rendered.endswith("+00:00"):
        rendered = rendered[: -len("+00:00")] + "Z"
    return rendered


# --- file discovery -------------------------------------------------------


def _iter_transcript_files(root: Path, session_id: str | None) -> list[Path]:
    """Return the transcript files under ``root`` to consider, sorted.

    Walks every per-project subdirectory of ``root`` for ``*.jsonl`` files. A
    missing ``root`` yields an empty list. When ``session_id`` is given, only
    files whose stem equals it are returned (so a single-session audit reads
    just that file). Results are sorted by path for deterministic order.
    """
    if not root.is_dir():
        return []
    matches: list[Path] = []
    for path in sorted(root.rglob(f"*{JSONL_SUFFIX}")):
        if not path.is_file():
            continue
        if session_id is not None and path.stem != session_id:
            continue
        matches.append(path)
    return matches


# --- public API -----------------------------------------------------------


def load_transcripts(
    *,
    root: Path | str | None = None,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    session_id: str | None = None,
) -> LoadResult:
    """Load + normalize session transcripts for a window into a turn list.

    Pure read: opens each matching file once, writes nothing, and returns
    frozen :class:`TranscriptTurn` values. Tolerant of a missing root,
    unreadable files, blank lines, non-JSON lines, non-turn records, and
    records missing fields — each is dropped and tallied in the result rather
    than raised.

    Args:
        root: The session-transcript root. Defaults to
            :func:`default_projects_root` (``$CLAUDE_PROJECTS_DIR`` or
            ``~/.claude/projects``).
        since: Inclusive lower time bound (ISO-8601 string with ``Z``/offset,
            or an aware :class:`datetime.datetime`). Turns at or after this
            instant are kept. Turns with **no** timestamp are kept regardless
            of bounds (they can't be excluded by time, and dropping them would
            hide content from the audit). ``None`` = no lower bound.
        until: Exclusive upper time bound, same accepted forms. ``None`` = no
            upper bound.
        session_id: When given, only the transcript whose filename stem matches
            is read. ``None`` = every session under ``root``.

    Returns:
        A :class:`LoadResult` with the kept turns (chronological, no-timestamp
        first) plus read/skip tallies.
    """
    base = default_projects_root() if root is None else Path(root)
    since_instant = _as_instant(since) if since is not None else None
    until_instant = _as_instant(until) if until is not None else None

    turns: list[TranscriptTurn] = []
    files_read = 0
    skipped = 0
    malformed_files = 0

    for path in _iter_transcript_files(base, session_id):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            malformed_files += 1
            continue
        files_read += 1
        fallback_session_id = path.stem
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except (ValueError, TypeError):
                skipped += 1
                continue
            turn = _normalize_record(record, fallback_session_id)
            if turn is None:
                skipped += 1
                continue
            if not _within_window(turn, since_instant, until_instant):
                continue
            turns.append(turn)

    turns.sort(key=_turn_sort_key)
    return LoadResult(
        turns=turns,
        files_read=files_read,
        skipped_records=skipped,
        malformed_files=malformed_files,
    )


def chunk_turns(
    turns: Iterable[TranscriptTurn],
    *,
    max_chars: int | None = None,
    max_tokens: int | None = None,
) -> list[TranscriptChunk]:
    """Split turns into consecutive, budget-bounded :class:`TranscriptChunk`s.

    Greedy, order-preserving packing: turns are accumulated into the current
    chunk until adding the next would exceed the character budget, then a new
    chunk starts. A single turn larger than the budget gets its own chunk
    (never split mid-turn — the model still sees a coherent turn).

    Args:
        turns: The turns to chunk (typically :attr:`LoadResult.turns`).
        max_chars: The per-chunk character budget. Mutually exclusive with
            ``max_tokens``.
        max_tokens: A token budget, converted to characters via
            :data:`CHARS_PER_TOKEN`. Mutually exclusive with ``max_chars``.

    Returns:
        The chunks in order; empty when ``turns`` is empty.

    Raises:
        ValueError: if neither or both of ``max_chars`` / ``max_tokens`` are
            given, or if the resolved budget is not positive.
    """
    if (max_chars is None) == (max_tokens is None):
        raise ValueError("pass exactly one of max_chars or max_tokens")
    budget = max_chars if max_chars is not None else max_tokens * CHARS_PER_TOKEN  # type: ignore[operator]
    if budget <= 0:
        raise ValueError("chunk budget must be positive")

    chunks: list[TranscriptChunk] = []
    current: list[TranscriptTurn] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(
                TranscriptChunk(turns=tuple(current), char_len=current_len, index=len(chunks))
            )
            current = []
            current_len = 0

    for turn in turns:
        turn_len = turn.char_len
        if current and current_len + turn_len > budget:
            flush()
        current.append(turn)
        current_len += turn_len
    flush()
    return chunks


# --- internal helpers -----------------------------------------------------


def _as_instant(bound: str | datetime) -> datetime:
    """Normalize a time bound to an aware UTC datetime (rejects naive)."""
    if isinstance(bound, datetime):
        if bound.tzinfo is None:
            raise ValueError("time bound datetime must be timezone-aware")
        return parse_time(bound.isoformat())
    return parse_time(bound)


def _within_window(
    turn: TranscriptTurn, since: datetime | None, until: datetime | None
) -> bool:
    """Whether a turn falls in ``[since, until)``; timeless turns always pass.

    A turn with no parseable timestamp can't be placed on the timeline, so it
    is kept regardless of the bounds — excluding it would silently hide content
    the audit may need. Bounded turns honor inclusive-``since`` /
    exclusive-``until``.
    """
    if turn.time is None:
        return True
    if since is None and until is None:
        return True
    instant = parse_time(turn.time)
    if since is not None and instant < since:
        return False
    if until is not None and instant >= until:
        return False
    return True


#: A far-past sentinel instant so timeless turns sort before any real one
#: without special-casing ``None`` inside the comparison.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _turn_sort_key(turn: TranscriptTurn) -> tuple[datetime, str]:
    """Order key: parsed instant (timeless turns first), then session id.

    Sorting on the *parsed* instant — not the raw string — keeps order correct
    across mixed sub-second precision (``...00Z`` vs ``...00.5Z`` compare
    wrong lexically). Timeless turns map to :data:`_EPOCH` so they sort ahead
    of every timestamped turn; the session id is a stable tiebreaker so
    equal-instant turns keep a deterministic order.
    """
    instant = _EPOCH if turn.time is None else parse_time(turn.time)
    return (instant, turn.session_id)
