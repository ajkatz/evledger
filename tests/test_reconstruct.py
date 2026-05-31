"""Unit tests for :mod:`evledger.reconstruct` — the model-backed
reconstruction + necessity layer.

The model call sits behind an injectable :class:`ModelClient`; every test here
passes a **fake client** returning canned structured output, so the whole
deterministic layer (prompt construction, response parsing, provenance
stamping, signature + dedup, orchestration) is exercised with **no network and
no model**. The import-guarded Agent SDK is never touched by these tests.

The fixtures mirror the live-emit convention's ``data`` shapes
(``decision.autonomous`` / ``failure`` / ``refusal``) so a reconstructed event
is byte-for-byte the same shape as a self-emitted one — only its ``source`` and
the ``reconstructed`` / ``session_id`` / ``audit_source`` provenance markers
differ.
"""

from __future__ import annotations

import json

from evledger import (
    AUDIT_SOURCE,
    AuditResult,
    CandidateEvent,
    LedgerEvent,
    NecessityLabel,
    ReconstructConfig,
    build_audit_prompt,
    chunk_turns,
    dedup_candidates,
    existing_signatures,
    new_event,
    parse_audit_response,
    reconstruct_chunks,
    to_ledger_event,
)
from evledger.reconstruct import (
    AUDIT_SOURCE_KEY,
    AUDIT_SYSTEM_PROMPT,
    RECONSTRUCTED_KEY,
    SESSION_ID_KEY,
)
from evledger.transcript import TranscriptTurn

# --- fakes + builders -----------------------------------------------------


class FakeModelClient:
    """A canned :class:`ModelClient`: returns scripted responses per call.

    Records the (system, prompt) pairs it was called with so a test can assert
    the prompt the layer built. ``responses`` is consumed in order; once
    exhausted it returns ``"{}"`` (an empty-but-valid audit).
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self._responses:
            return self._responses.pop(0)
        return "{}"


class RaisingModelClient:
    """A :class:`ModelClient` whose ``complete`` always raises."""

    def complete(self, *, system: str, prompt: str) -> str:
        raise RuntimeError("model exploded")


def _turn(role: str, text: str, *, time: str | None = "2026-05-30T10:00:00Z") -> TranscriptTurn:
    return TranscriptTurn(session_id="s1", role=role, text=text, time=time)


def _cand(
    kind: str, data: dict, *, session_id: str = "s1", time: str | None = None
) -> CandidateEvent:
    """Terse :class:`CandidateEvent` builder for the tests."""
    return CandidateEvent(kind=kind, data=data, session_id=session_id, time=time)


def _decision_response(summary: str, *, could_have_asked: bool = True) -> str:
    """A canned model response with one decision.autonomous candidate."""
    return json.dumps(
        {
            "events": [
                {
                    "kind": "decision.autonomous",
                    "data": {
                        "summary": summary,
                        "rationale": "the transcript shows an unflagged fork",
                        "could_have_asked": could_have_asked,
                    },
                    "time": "2026-05-30T10:05:00Z",
                }
            ],
            "necessity": [],
        }
    )


# --- prompt construction --------------------------------------------------


def test_system_prompt_pins_the_three_kinds_and_json_only_contract() -> None:
    # The load-bearing parts of the contract the parser depends on.
    assert "decision.autonomous" in AUDIT_SYSTEM_PROMPT
    assert "failure" in AUDIT_SYSTEM_PROMPT
    assert "refusal" in AUDIT_SYSTEM_PROMPT
    assert "necessity" in AUDIT_SYSTEM_PROMPT
    assert "needed" in AUDIT_SYSTEM_PROMPT and "rote" in AUDIT_SYSTEM_PROMPT
    assert "ONLY the JSON" in AUDIT_SYSTEM_PROMPT


def test_build_audit_prompt_renders_turns_in_order_with_role_and_time() -> None:
    chunk = chunk_turns(
        [
            _turn("user", "please refactor the auth module"),
            _turn("assistant", "I'll split it into two files", time="2026-05-30T10:01:00Z"),
        ],
        max_chars=10_000,
    )[0]

    prompt = build_audit_prompt(chunk)

    assert "USER: please refactor the auth module" in prompt
    assert "ASSISTANT: I'll split it into two files" in prompt
    # The user turn (earlier) precedes the assistant turn in the rendering.
    assert prompt.index("USER:") < prompt.index("ASSISTANT:")
    # Timestamps are surfaced so the model can attribute findings.
    assert "[2026-05-30T10:00:00Z]" in prompt
    # The chunk index is named.
    assert "chunk 0" in prompt


def test_build_audit_prompt_omits_timestamp_for_timeless_turn() -> None:
    chunk = chunk_turns([_turn("user", "no clock here", time=None)], max_chars=10_000)[0]
    prompt = build_audit_prompt(chunk)
    assert "USER: no clock here" in prompt
    assert "[None]" not in prompt and "[]" not in prompt


# --- response parsing -----------------------------------------------------


def test_parse_clean_json_yields_candidate_and_necessity() -> None:
    raw = json.dumps(
        {
            "events": [
                {
                    "kind": "failure",
                    "data": {"kind": "test", "summary": "auth test failed", "context": "null id"},
                }
            ],
            "necessity": [
                {
                    "target_kind": "question.asked",
                    "label": "rote",
                    "summary": "asked which file to edit when only one existed",
                    "rationale": "no real alternative",
                }
            ],
        }
    )

    result = parse_audit_response(raw, session_id="s1")

    assert isinstance(result, AuditResult)
    assert result.parse_errors == 0
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.kind == "failure"
    assert cand.data["summary"] == "auth test failed"
    assert cand.session_id == "s1"
    assert len(result.necessity) == 1
    assert result.necessity[0].label == "rote"
    assert result.necessity[0].session_id == "s1"


def test_parse_tolerates_a_json_code_fence() -> None:
    inner = _decision_response("chose composition over inheritance")
    raw = f"Here is my audit:\n```json\n{inner}\n```\nDone."
    result = parse_audit_response(raw, session_id="s1")
    assert result.parse_errors == 0
    assert len(result.candidates) == 1
    assert result.candidates[0].data["summary"] == "chose composition over inheritance"


def test_parse_tolerates_prose_wrapping_via_brace_scan() -> None:
    inner = _decision_response("picked sqlite over postgres")
    raw = f"Sure! {inner} Let me know if you need more."
    result = parse_audit_response(raw, session_id="s1")
    assert result.parse_errors == 0
    assert len(result.candidates) == 1


def test_parse_unrecoverable_response_degrades_to_parse_error() -> None:
    result = parse_audit_response("I could not find any issues, sorry.", session_id="s1")
    assert result.candidates == []
    assert result.necessity == []
    assert result.parse_errors == 1


def test_parse_drops_unknown_kind_and_bad_necessity_label_counting_errors() -> None:
    raw = json.dumps(
        {
            "events": [
                {"kind": "made.up.kind", "data": {"summary": "x"}},
                {"kind": "refusal", "data": {"action": "rm -rf /", "reason": "destructive"}},
            ],
            "necessity": [
                {
                    "target_kind": "question.asked",
                    "label": "maybe",
                    "summary": "s",
                    "rationale": "r",
                },
            ],
        }
    )
    result = parse_audit_response(raw, session_id="s1")
    # The made-up kind and the out-of-vocab label are both dropped + counted.
    assert result.parse_errors == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].kind == "refusal"
    assert result.necessity == []


def test_parse_brace_scan_respects_braces_inside_string_values() -> None:
    # A `}` inside a string value must not close the object early during the
    # brace-depth fallback scan.
    inner = json.dumps(
        {
            "events": [
                {"kind": "refusal", "data": {"action": "echo } now", "reason": "x"}}
            ],
            "necessity": [],
        }
    )
    raw = f"some noise {inner} trailing words"
    result = parse_audit_response(raw, session_id="s1")
    assert result.parse_errors == 0
    assert len(result.candidates) == 1
    assert result.candidates[0].data["action"] == "echo } now"


def test_parse_missing_lists_is_empty_not_error() -> None:
    result = parse_audit_response("{}", session_id="s1")
    assert result.candidates == []
    assert result.necessity == []
    assert result.parse_errors == 0


# --- materialization + provenance -----------------------------------------


def test_to_ledger_event_stamps_source_and_provenance_markers() -> None:
    candidate = CandidateEvent(
        kind="decision.autonomous",
        data={"summary": "chose to refactor not patch", "could_have_asked": True},
        session_id="sess-abc",
        time="2026-05-30T10:05:00Z",
    )

    event = to_ledger_event(candidate, machine="laptop", id="fixedid")

    assert isinstance(event, LedgerEvent)
    # source distinguishes audit-found from live self-emitted.
    assert event.source == AUDIT_SOURCE
    assert event.type == "dev.claude.decision.autonomous"
    assert event.machine == "laptop"
    assert event.time == "2026-05-30T10:05:00Z"
    # provenance markers on data.
    assert event.data[RECONSTRUCTED_KEY] is True
    assert event.data[SESSION_ID_KEY] == "sess-abc"
    assert event.data[AUDIT_SOURCE_KEY] == AUDIT_SOURCE
    # original content preserved.
    assert event.data["summary"] == "chose to refactor not patch"
    # seq unset until the store assigns it.
    assert event.seq is None


def test_to_ledger_event_does_not_mutate_the_candidate() -> None:
    candidate = CandidateEvent(
        kind="failure",
        data={"kind": "build", "summary": "tsc failed"},
        session_id="s1",
    )
    to_ledger_event(candidate, machine="m")
    # The candidate's own data is untouched (no provenance leaked back).
    assert RECONSTRUCTED_KEY not in candidate.data
    assert SESSION_ID_KEY not in candidate.data


# --- signature ------------------------------------------------------------


def test_signature_is_stable_under_whitespace_and_case() -> None:
    a = _cand("failure", {"kind": "test", "summary": "Auth  Test\nFailed"})
    b = _cand("failure", {"kind": "test", "summary": "auth test failed"})
    assert a.signature == b.signature


def test_signature_differs_on_load_bearing_content() -> None:
    a = _cand("refusal", {"action": "force push", "reason": "x"})
    b = _cand("refusal", {"action": "mass delete", "reason": "x"})
    assert a.signature != b.signature


def test_signature_ignores_incidental_optional_fields() -> None:
    # decision.autonomous keys only on `summary` (its sole required field), so
    # a differing rationale does NOT change the signature.
    a = _cand("decision.autonomous", {"summary": "s", "rationale": "one"})
    b = _cand("decision.autonomous", {"summary": "s", "rationale": "two"})
    assert a.signature == b.signature


# --- existing_signatures + dedup ------------------------------------------


def _stored_reconstructed_event(kind_type: str, data: dict, session_id: str) -> LedgerEvent:
    """Build a stored reconstructed event the way to_ledger_event would have."""
    payload = dict(data)
    payload[RECONSTRUCTED_KEY] = True
    payload[SESSION_ID_KEY] = session_id
    payload[AUDIT_SOURCE_KEY] = AUDIT_SOURCE
    return new_event(
        source=AUDIT_SOURCE,
        type=kind_type,
        machine="m",
        data=payload,
        id="i",
        time="2026-05-30T10:00:00Z",
    )


def test_existing_signatures_indexes_only_reconstructed_events() -> None:
    reconstructed = _stored_reconstructed_event(
        "dev.claude.failure", {"kind": "test", "summary": "boom"}, "s1"
    )
    # A live self-emitted event (no reconstructed marker) must NOT be indexed —
    # the audit complements self-reports, it doesn't suppress them.
    live = new_event(
        source="claude-config",
        type="dev.claude.failure",
        machine="m",
        data={"kind": "test", "summary": "boom"},
        id="j",
        time="2026-05-30T10:00:00Z",
    )

    sigs = existing_signatures([reconstructed, live])

    assert len(sigs) == 1
    assert ("s1", _sig_of("failure", {"kind": "test", "summary": "boom"})) in sigs


def _sig_of(kind: str, data: dict) -> str:
    from evledger.reconstruct import _signature_for

    return _signature_for(kind, data)


def test_dedup_skips_candidate_already_in_ledger() -> None:
    stored = _stored_reconstructed_event(
        "dev.claude.failure", {"kind": "test", "summary": "boom"}, "s1"
    )
    existing = existing_signatures([stored])

    fresh = _cand("failure", {"kind": "test", "summary": "boom"})
    novel = _cand("failure", {"kind": "test", "summary": "different"})

    out = dedup_candidates([fresh, novel], existing)

    assert len(out) == 1
    assert out[0].data["summary"] == "different"


def test_dedup_same_signature_different_session_is_kept() -> None:
    stored = _stored_reconstructed_event(
        "dev.claude.failure", {"kind": "test", "summary": "boom"}, "s1"
    )
    existing = existing_signatures([stored])
    # Same content, DIFFERENT session -> distinct (session_id, signature) key.
    other = _cand("failure", {"kind": "test", "summary": "boom"}, session_id="s2")
    out = dedup_candidates([other], existing)
    assert len(out) == 1


def test_dedup_collapses_duplicates_within_a_single_run() -> None:
    a = _cand("refusal", {"action": "force push", "reason": "history"})
    b = _cand("refusal", {"action": "force  push", "reason": "history"})
    out = dedup_candidates([a, b], set())
    assert len(out) == 1


def test_round_trip_materialized_event_dedups_against_itself() -> None:
    # Materialize a candidate, "store" it, then re-audit: the same candidate
    # must be recognized as already-present (the idempotency invariant).
    candidate = _cand("decision.autonomous", {"summary": "renamed the action generically"})
    stored = to_ledger_event(candidate, machine="m", id="x")
    existing = existing_signatures([stored])
    out = dedup_candidates([candidate], existing)
    assert out == []


# --- orchestration --------------------------------------------------------


def test_reconstruct_chunks_calls_client_per_chunk_and_merges() -> None:
    turns = [_turn("user", "u" * 50), _turn("assistant", "a" * 50)]
    chunks = chunk_turns(turns, max_chars=40)  # forces 2 chunks
    assert len(chunks) == 2

    client = FakeModelClient(
        [
            _decision_response("first finding"),
            json.dumps(
                {
                    "events": [
                        {
                            "kind": "refusal",
                            "data": {"action": "drop table", "reason": "destructive"},
                        }
                    ],
                    "necessity": [],
                }
            ),
        ]
    )

    result = reconstruct_chunks(chunks, client, session_id="s1")

    assert len(client.calls) == 2
    # Each call used the audit system prompt.
    assert all(system == AUDIT_SYSTEM_PROMPT for system, _ in client.calls)
    assert len(result.candidates) == 2
    kinds = {c.kind for c in result.candidates}
    assert kinds == {"decision.autonomous", "refusal"}
    assert result.parse_errors == 0


def test_reconstruct_chunks_does_not_dedup_returns_raw_merge() -> None:
    turns = [_turn("user", "x" * 50), _turn("assistant", "y" * 50)]
    chunks = chunk_turns(turns, max_chars=40)
    dup = _decision_response("same finding")
    client = FakeModelClient([dup, dup])
    result = reconstruct_chunks(chunks, client, session_id="s1")
    # Same finding twice — reconstruct_chunks merges WITHOUT dedup (dedup is a
    # separate, ledger-aware step).
    assert len(result.candidates) == 2
    deduped = dedup_candidates(result.candidates, set())
    assert len(deduped) == 1


def test_reconstruct_chunks_survives_a_raising_client() -> None:
    chunks = chunk_turns([_turn("user", "hello")], max_chars=10_000)
    result = reconstruct_chunks(chunks, RaisingModelClient(), session_id="s1")
    assert result.candidates == []
    assert result.parse_errors == 1


def test_necessity_label_carries_session_provenance() -> None:
    raw = json.dumps(
        {
            "events": [],
            "necessity": [
                {
                    "target_kind": "permission.requested",
                    "label": "needed",
                    "summary": "asked before force-pushing",
                    "rationale": "rewrites published history",
                }
            ],
        }
    )
    result = parse_audit_response(raw, session_id="sess-9")
    assert len(result.necessity) == 1
    label = result.necessity[0]
    assert isinstance(label, NecessityLabel)
    assert label.target_kind == "permission.requested"
    assert label.label == "needed"
    assert label.session_id == "sess-9"


# --- config override ------------------------------------------------------


def test_config_override_maps_kinds_to_custom_types() -> None:
    cfg = ReconstructConfig(
        event_types={
            "decision.autonomous": "x.decision",
            "failure": "x.failure",
            "refusal": "x.refusal",
        },
        audit_source="my-auditor",
    )
    candidate = _cand("failure", {"kind": "bug", "summary": "off by one"})
    event = to_ledger_event(candidate, cfg, machine="m")
    assert event.type == "x.failure"
    assert event.source == "my-auditor"
    assert event.data[AUDIT_SOURCE_KEY] == "my-auditor"
