"""A git-backed, CloudEvents-aligned, append-only event ledger.

This package is a **standalone component**: it imports nothing from sibling
``claude_kg`` modules and depends only on the standard library, so it can be
extracted to its own package/repo later (extraction is a move, not a
rewrite). ``claude-config`` is its first consumer, not its owner — there are
no hardcoded ``~/.claude`` paths or claude-specific event taxonomy in the
core; every root, machine-id source, and naming convention is supplied via a
config argument with a generic default.

This module is the **public, semver-able API surface** — the symbols an
adopter imports. Internals live in the submodules (:mod:`~.schema`,
:mod:`~.machine`, :mod:`~.registry`) and may change without notice.

Schema layer (this task, ``ledger-schema``)::

    from evledger import new_event, resolve_machine_id

    machine = resolve_machine_id()
    event = new_event(
        source=f"/{machine}/dev",
        type="dev.example.thing.start",
        machine=machine,
        data={"detail": "..."},
    )
    line = event.to_jsonl_line()  # one CloudEvent, ready to append

Events are point-in-time instants (no ``duration``); durations are derived
downstream by pairing ``*.start`` / ``*.end`` events.

Store layer (``ledger-store``)::

    from evledger import LedgerStore

    store = LedgerStore(root="/path/to/ledger")
    stored = store.append(event)        # assigns monotonic per-machine seq
    result = store.read_all()           # ReadResult(events=[...], malformed=N)

``append`` writes one CloudEvent line to
``<root>/<machine-id>/<YYYY-MM>.jsonl`` (machine-partitioned, time-bucketed),
assigns the next per-machine ``seq``, and is atomic + safe under concurrent
same-machine appends. ``read_all`` / ``iter_events`` parse all partitions,
tolerating a missing root and dropping (and counting) malformed lines.

Query layer (``ledger-query``)::

    from evledger import LedgerStore, Query, query_events

    store = LedgerStore(root="/path/to/ledger")
    hits = query_events(
        store.iter_events(),
        Query(machine="laptop", type="dev.example.mission.*",
              since="2026-06-01T00:00:00Z"),
    )

:func:`query_events` is a **pure** filter over any iterable of events (no new
persistence): criteria are ANDed, ``type`` accepts a glob, ``since`` is an
inclusive / ``until`` an exclusive time bound, and results come back ordered by
``(time, seq)``.

Stats layer (``ledger-stats``)::

    from evledger import (
        counts_by_type, event_rate, pair_events, DurationStats,
    )
    from datetime import timedelta

    events = query_events(store.iter_events(), Query(type="dev.example.*"))

    by_type = counts_by_type(events)                 # {type: count}
    rate = event_rate(events, since="2026-06-01T00:00:00Z",
                      until="2026-07-01T00:00:00Z")
    per_hour = rate.per(timedelta(hours=1))          # events / hour

    pairing = pair_events(events)                    # *.start -> *.end
    durations = DurationStats.from_pairing(pairing)  # count/total/min/max/...

The stats layer is **pure** and **descriptive only** — counts, rates, and
paired ``*.start`` / ``*.end`` durations. No inference (prediction, anomaly
detection, trends) lives here; that is the deferred ``ledger-inference`` task.

Digest layer (``ledger-digest``)::

    from evledger import digest, CLAUDE_DIGEST_CONFIG

    events = query_events(store.iter_events(), Query(since="..."))
    report = digest(events, CLAUDE_DIGEST_CONFIG)
    report.rollup.total                  # counts / tokens / cost / durations
    [a.message for a in report.anomalies]  # rule-based flags

The digest layer is **deterministic** — a rollup plus rule-based anomaly flags
computed purely over the event stream, **no model**. It stays taxonomy-free: a
:class:`DigestConfig` names the event types / data keys the rules look for
(:data:`CLAUDE_DIGEST_CONFIG` wires the ``dev.claude.*`` names for the
claude-config consumer). Each rule is a pure function; :func:`digest` composes
the rollup and every rule into one :class:`Digest`.

Transcript-loader layer (``audit-transcript-loader``)::

    from evledger import load_transcripts, chunk_turns

    result = load_transcripts(since="2026-06-01T00:00:00Z")  # ~/.claude/projects
    chunks = chunk_turns(result.turns, max_tokens=20_000)

:func:`load_transcripts` locates the on-disk Claude Code session JSONL files
for a time window (optionally one session id), normalizes the ``user`` /
``assistant`` turns into a minimal frozen :class:`TranscriptTurn` list, and is
tolerant of a missing root / unreadable files / malformed records (dropped and
counted in :class:`LoadResult`). :func:`chunk_turns` greedily packs the turns
into budget-bounded :class:`TranscriptChunk`s for a model. This layer is
**pure + model-free** — the deterministic plumbing under the model-powered
audit, unit-testable without a live model.

Reconstruction + necessity layer (``audit-reconstruct``)::

    from evledger import (
        ReconstructConfig, build_audit_prompt, parse_audit_response,
        reconstruct_chunks, to_ledger_event, existing_signatures,
        dedup_candidates, resolve_machine_id, LedgerStore,
    )

    result = reconstruct_chunks(chunks, client, session_id="abc")
    existing = existing_signatures(store.read_all().events)
    fresh = dedup_candidates(result.candidates, existing)
    events = [to_ledger_event(c, machine=resolve_machine_id()) for c in fresh]

This layer turns transcript chunks into reconstructed oversight events +
necessity verdicts via an **injectable** :class:`ModelClient` (real =
:class:`AgentSdkModelClient` over the import-guarded Claude Agent SDK; tests =
a fake returning canned JSON). Reconstructed events carry provenance markers
(``source="oversight-analyzer"``, ``data.reconstructed=True``,
``data.session_id``, ``data.audit_source``) that distinguish them from live
self-emitted events, and are deduped against the existing ledger by
``(session_id, signature)`` so re-auditing never double-emits. It is
append-only and best-effort — a missing SDK / credential or a mangled response
degrades to no candidates, never an exception.

Public symbols
--------------
:class:`LedgerEvent`
    The frozen, CloudEvents 1.0-aligned event envelope.
:func:`new_event`
    Factory that fills in generated ``id`` / ``time`` defaults.
:func:`now_utc`, :func:`format_time`, :func:`parse_time`
    ISO-8601 UTC (``Z``-suffixed) time helpers.
:func:`resolve_machine_id`
    Resolve the machine partition key (env -> id-file -> hostname).
:class:`TypeRegistry`, :class:`SchemaValidationError`
    Optional per-type ``data`` schema registry and its error type.
:class:`LedgerStore`
    Append-only, atomic, multi-writer-safe JSONL writer + reader.
:class:`ReadResult`
    The result of reading the ledger: parsed ``events`` + ``malformed`` count.
:class:`Query`
    A frozen filter description (source / type-glob / machine / since / until).
:func:`query_events`
    Pure filter over an event iterable, returning matches ordered by
    ``(time, seq)``.
:func:`count_by`, :func:`counts_by_source`, :func:`counts_by_type`
    Tally events by a key / by ``source`` / by ``type``.
:func:`event_rate`, :class:`EventRate`
    Events-per-unit-time over an observed or explicit-window span.
:func:`pair_events`, :class:`Pairing`, :class:`PairedDuration`
    Match ``*.start`` / ``*.end`` events into durations.
:class:`DurationStats`
    Descriptive summary (count/total/min/max/mean/median) of durations.
:class:`DigestConfig`, :data:`CLAUDE_DIGEST_CONFIG`
    The (taxonomy-free) names the digest rules look for, and a ready-wired
    ``dev.claude.*`` config for the claude-config consumer.
:func:`rollup`, :class:`Rollup`, :class:`ResourceTotals`, :class:`Notable`
    The deterministic rollup over a window (counts / resource totals /
    durations / recent notables).
:func:`flag_could_have_asked`, :func:`flag_failures`,
:func:`flag_push_without_green`, :func:`flag_token_outliers`,
:func:`flag_refusals`, :func:`anomalies`, :class:`Anomaly`
    The rule-based anomaly flags and the helper that runs them all.
:func:`digest`, :class:`Digest`
    The composed digest: rollup + every anomaly flag.
:func:`load_transcripts`, :class:`LoadResult`, :class:`TranscriptTurn`
    Window-scoped session-transcript loader → minimal normalized turn list.
:func:`chunk_turns`, :class:`TranscriptChunk`
    Greedy, budget-bounded chunking of turns for a model.
:func:`default_projects_root`
    The default ``~/.claude/projects`` (or ``$CLAUDE_PROJECTS_DIR``) root.
"""

from __future__ import annotations

from evledger.digest import (
    CLAUDE_DIGEST_CONFIG,
    Anomaly,
    Digest,
    DigestConfig,
    Notable,
    ResourceTotals,
    Rollup,
    anomalies,
    digest,
    flag_could_have_asked,
    flag_failures,
    flag_push_without_green,
    flag_refusals,
    flag_token_outliers,
    rollup,
)
from evledger.machine import resolve_machine_id
from evledger.paths import (
    LEDGER_ROOT_ENV_VAR,
    LEGACY_LEDGER_ROOT_ENV_VARS,
    default_ledger_root,
    env_ledger_root,
)
from evledger.query import Query, query_events
from evledger.reconstruct import (
    AUDIT_SOURCE,
    AgentSdkModelClient,
    AuditResult,
    CandidateEvent,
    ModelClient,
    ModelUnavailableError,
    NecessityLabel,
    ReconstructConfig,
    build_audit_prompt,
    dedup_candidates,
    existing_signatures,
    parse_audit_response,
    reconstruct_chunks,
    to_ledger_event,
)
from evledger.registry import SchemaValidationError, TypeRegistry
from evledger.schema import (
    LedgerEvent,
    format_time,
    new_event,
    now_utc,
    parse_time,
)
from evledger.stats import (
    DurationStats,
    EventRate,
    PairedDuration,
    Pairing,
    count_by,
    counts_by_source,
    counts_by_type,
    event_rate,
    pair_events,
)
from evledger.store import LedgerStore, ReadResult
from evledger.transcript import (
    LoadResult,
    TranscriptChunk,
    TranscriptTurn,
    chunk_turns,
    default_projects_root,
    load_transcripts,
)

__all__ = [
    "LedgerEvent",
    "new_event",
    "now_utc",
    "format_time",
    "parse_time",
    "resolve_machine_id",
    "TypeRegistry",
    "SchemaValidationError",
    "LedgerStore",
    "ReadResult",
    "default_ledger_root",
    "env_ledger_root",
    "LEDGER_ROOT_ENV_VAR",
    "LEGACY_LEDGER_ROOT_ENV_VARS",
    "Query",
    "query_events",
    "count_by",
    "counts_by_source",
    "counts_by_type",
    "event_rate",
    "EventRate",
    "pair_events",
    "Pairing",
    "PairedDuration",
    "DurationStats",
    # digest (deterministic oversight: rollup + anomaly rules)
    "DigestConfig",
    "CLAUDE_DIGEST_CONFIG",
    "Rollup",
    "ResourceTotals",
    "Notable",
    "Anomaly",
    "Digest",
    "rollup",
    "anomalies",
    "digest",
    "flag_could_have_asked",
    "flag_failures",
    "flag_push_without_green",
    "flag_token_outliers",
    "flag_refusals",
    # transcript loader (model-free plumbing under the model-powered audit)
    "load_transcripts",
    "LoadResult",
    "TranscriptTurn",
    "chunk_turns",
    "TranscriptChunk",
    "default_projects_root",
    # reconstruction + necessity (model-backed, behind an injectable client)
    "ModelClient",
    "AgentSdkModelClient",
    "ModelUnavailableError",
    "ReconstructConfig",
    "CandidateEvent",
    "NecessityLabel",
    "AuditResult",
    "AUDIT_SOURCE",
    "build_audit_prompt",
    "parse_audit_response",
    "reconstruct_chunks",
    "to_ledger_event",
    "existing_signatures",
    "dedup_candidates",
]
