"""Deterministic oversight digest over a stream of ledger events.

This module is the **digest path** of the ledger (the ``ledger-digest`` task of
the ``oversight-digest-anomaly-alerts-deterministic`` whim). It turns the
ledger from a passive log into *active oversight* — but the **deterministic**
half only: a rollup plus rule-based anomaly flags computed purely over the
event stream. There is **no model** here; every output is plain computation
over the events (the model-powered transcript audit lives in a separate whim).

Like the rest of the ledger it is *pure*: every function takes any iterable of
:class:`~evledger.schema.LedgerEvent` (typically the output of
:func:`~evledger.query.query_events`, but any iterable works), reads it
once, writes nothing, and never mutates the frozen events. It performs **no new
persistence** (v1 just reports; an optional ``alert`` emission is deferred).

Standalone + taxonomy-free
--------------------------
The ledger core imports nothing claude-specific and carries no ``dev.claude.*``
taxonomy (that vocabulary lives in :mod:`claude_kg.events`, never inside
``claude_kg/ledger/``). So this module does **not** hardcode any event-type
strings. Instead a :class:`DigestConfig` value object names the event types and
data keys the rules look for, exactly like :func:`~evledger.stats.pair_events`
parameterizes its ``.start`` / ``.end`` suffixes. The consumer (the CLI layer,
or :mod:`claude_kg.events`) supplies the concrete ``dev.claude.*`` names; the
generic defaults here are deliberately illustrative, not authoritative.

The digest has two halves:

* **Rollup** (:func:`rollup`) — counts by type; summed ``tokens`` / ``cost`` /
  ``duration_s`` drawn from the *shipped* enrichment events; mission/task
  durations via ``*.start`` / ``*.end`` pairing; and the most recent notable
  lines (autonomous decisions, failures, refusals).
* **Anomaly rules** (:func:`flag_could_have_asked`, :func:`flag_failures`,
  :func:`flag_push_without_green`, :func:`flag_token_outliers`,
  :func:`flag_refusals`) — each a pure function returning a list of
  :class:`Anomaly` flags. :func:`anomalies` runs them all.

:func:`digest` composes the rollup and every anomaly rule into a single
:class:`Digest` value object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from evledger.schema import LedgerEvent, parse_time
from evledger.stats import (
    DurationStats,
    counts_by_type,
    pair_events,
)

# --- configuration --------------------------------------------------------


@dataclass(frozen=True)
class DigestConfig:
    """Names the event types / data keys the digest rules look for.

    Keeps the ledger core taxonomy-free: the consumer supplies the concrete
    ``dev.claude.*`` strings. The defaults are generic, illustrative values —
    real callers (the ``ledger digest`` CLI) override them with the project's
    actual event vocabulary.

    Attributes:
        shipped_type: Event ``type`` carrying the per-task resource-accounting
            enrichment (``tokens`` / ``duration_s`` / ``cost`` data keys).
        decision_autonomous_type: Event ``type`` for an autonomous decision.
        failure_type: Event ``type`` for a caught failure (test/build/bug/tool).
        refusal_type: Event ``type`` for a declined / paused grave action.
        push_type: Event ``type`` for a VCS push.
        could_have_asked_key: ``data`` key (boolean) marking a decision the user
            might have wanted to be consulted on.
        tokens_key / duration_key / cost_key: ``data`` keys on the shipped event.
        start_suffix / end_suffix: type suffixes used to pair durations.
        outlier_factor: A shipped metric (``tokens`` or ``duration_s``) is an
            outlier when it exceeds ``outlier_factor`` times the window median.
        recent_limit: How many recent notable lines to keep per category.
    """

    shipped_type: str = "task.shipped"
    decision_autonomous_type: str = "decision.autonomous"
    failure_type: str = "failure"
    refusal_type: str = "refusal"
    push_type: str = "push"

    could_have_asked_key: str = "could_have_asked"
    tokens_key: str = "tokens"
    duration_key: str = "duration_s"
    cost_key: str = "cost"

    start_suffix: str = ".start"
    end_suffix: str = ".end"

    outlier_factor: float = 3.0
    recent_limit: int = 5


#: A ready-to-use config wired to the ``dev.claude.*`` taxonomy (see
#: :mod:`claude_kg.events`). The ledger *core* never depends on these names —
#: this constant is a convenience for consumers and lives here only so the CLI
#: and tests share one source of truth without importing the taxonomy module.
CLAUDE_DIGEST_CONFIG = DigestConfig(
    shipped_type="dev.claude.task.shipped",
    decision_autonomous_type="dev.claude.decision.autonomous",
    failure_type="dev.claude.failure",
    refusal_type="dev.claude.refusal",
    push_type="dev.claude.push",
)


# --- small helpers --------------------------------------------------------


def _data(event: LedgerEvent) -> dict[str, Any]:
    """Return the event's ``data`` as a dict (empty when absent / not a dict)."""
    return event.data if isinstance(event.data, dict) else {}


def _as_number(value: Any) -> float | None:
    """Coerce a numeric ``data`` value to ``float``, tolerating strings.

    Returns ``None`` when the value is missing, a bool (booleans are *not*
    treated as numbers here), or otherwise un-coercible. Best-effort: the
    digest reports over whatever data happens to be present.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _is_truthy_flag(value: Any) -> bool:
    """Interpret a ``data`` flag as a boolean, tolerating string spellings.

    ``True`` / ``"true"`` / ``"1"`` / ``"yes"`` (case-insensitive) read as true;
    everything else (including missing) reads as false. The live emitter is
    asked to send a real JSON boolean, but a stray string must not silently
    suppress the flag the auditor exists to catch.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


# --- rollup ---------------------------------------------------------------


@dataclass(frozen=True)
class ResourceTotals:
    """Summed resource accounting drawn from the shipped enrichment events.

    Attributes:
        shipped: Number of shipped events seen in the window.
        tokens: Summed ``tokens`` across shipped events that carried it.
        cost: Summed ``cost``.
        duration_s: Summed ``duration_s``.
    """

    shipped: int
    tokens: float
    cost: float
    duration_s: float


@dataclass(frozen=True)
class Notable:
    """One recent notable event, flattened for display.

    Attributes:
        time: The event ``time`` (ISO string, as stored).
        type: The event ``type``.
        source: The event ``source``.
        summary: A short human-readable summary drawn from the event ``data``.
    """

    time: str
    type: str
    source: str
    summary: str


@dataclass(frozen=True)
class Rollup:
    """The deterministic rollup over the window.

    Attributes:
        total: Total event count in the window.
        counts_by_type: Per-type event counts.
        resources: Summed tokens / cost / duration from shipped events.
        durations: Paired ``*.start`` / ``*.end`` duration summary.
        recent_decisions: Recent autonomous-decision notables (newest last).
        recent_failures: Recent failure notables (newest last).
        recent_refusals: Recent refusal notables (newest last).
    """

    total: int
    counts_by_type: dict[str, int]
    resources: ResourceTotals
    durations: DurationStats
    recent_decisions: tuple[Notable, ...]
    recent_failures: tuple[Notable, ...]
    recent_refusals: tuple[Notable, ...]


def _decision_summary(event: LedgerEvent) -> str:
    data = _data(event)
    summary = data.get("summary")
    text = str(summary) if summary is not None else "(no summary)"
    if _is_truthy_flag(data.get("could_have_asked")):
        text += "  [could_have_asked]"
    return text


def _failure_summary(event: LedgerEvent) -> str:
    data = _data(event)
    kind = data.get("kind")
    summary = data.get("summary")
    parts = []
    if kind is not None:
        parts.append(f"[{kind}]")
    parts.append(str(summary) if summary is not None else "(no summary)")
    return " ".join(parts)


def _refusal_summary(event: LedgerEvent) -> str:
    data = _data(event)
    action = data.get("action")
    reason = data.get("reason")
    action_text = str(action) if action is not None else "(no action)"
    reason_text = str(reason) if reason is not None else "(no reason)"
    return f"{action_text} — {reason_text}"


def _recent(
    events: Sequence[LedgerEvent],
    event_type: str,
    summarize: Callable[[LedgerEvent], str],
    limit: int,
) -> tuple[Notable, ...]:
    """Take the most recent ``limit`` events of ``event_type`` (newest last).

    ``events`` is assumed already ordered by ``(time, seq)``; we keep the tail
    so the newest sit at the end, matching ``ledger show`` ordering.
    """
    matched = [e for e in events if e.type == event_type]
    tail = matched[-limit:] if limit and limit > 0 else []
    return tuple(
        Notable(time=e.time, type=e.type, source=e.source, summary=summarize(e))
        for e in tail
    )


def _ordered(events: Iterable[LedgerEvent]) -> list[LedgerEvent]:
    """Return the events ordered by ``(time, seq)`` (seq=None sorts first)."""
    return sorted(
        events,
        key=lambda e: (parse_time(e.time), e.seq if e.seq is not None else -1),
    )


def rollup(events: Iterable[LedgerEvent], config: DigestConfig | None = None) -> Rollup:
    """Compute the deterministic rollup over ``events``.

    Pure over any (one-shot) iterable. Counts events by type, sums the shipped
    resource metrics, derives paired ``*.start`` / ``*.end`` durations, and
    collects the most recent autonomous-decision / failure / refusal notables.

    Args:
        events: Any iterable of events (typically a window from ``query_events``).
        config: Names the event types / data keys to look for. Defaults to the
            generic :class:`DigestConfig` — real callers pass one wired to their
            taxonomy (see :data:`CLAUDE_DIGEST_CONFIG`).

    Returns:
        A :class:`Rollup` value object.
    """
    cfg = config if config is not None else DigestConfig()
    ordered = _ordered(events)

    by_type = counts_by_type(ordered)

    shipped = [e for e in ordered if e.type == cfg.shipped_type]

    def _sum_metric(metric_key: str) -> float:
        total = 0.0
        for e in shipped:
            value = _as_number(_data(e).get(metric_key))
            if value is not None:
                total += value
        return total

    resources = ResourceTotals(
        shipped=len(shipped),
        tokens=_sum_metric(cfg.tokens_key),
        cost=_sum_metric(cfg.cost_key),
        duration_s=_sum_metric(cfg.duration_key),
    )

    pairing = pair_events(
        ordered, start_suffix=cfg.start_suffix, end_suffix=cfg.end_suffix
    )
    durations = DurationStats.from_pairing(pairing)

    return Rollup(
        total=len(ordered),
        counts_by_type=by_type,
        resources=resources,
        durations=durations,
        recent_decisions=_recent(
            ordered, cfg.decision_autonomous_type, _decision_summary, cfg.recent_limit
        ),
        recent_failures=_recent(
            ordered, cfg.failure_type, _failure_summary, cfg.recent_limit
        ),
        recent_refusals=_recent(
            ordered, cfg.refusal_type, _refusal_summary, cfg.recent_limit
        ),
    )


# --- anomaly rules --------------------------------------------------------


@dataclass(frozen=True)
class Anomaly:
    """A single rule-based anomaly flag.

    Attributes:
        rule: A stable rule identifier (e.g. ``"could_have_asked"``).
        severity: A coarse severity hint (``"info"`` / ``"warn"`` / ``"alert"``).
        message: A human-readable description of the flagged condition.
        event: The triggering event, when the flag points at one specific event
            (``None`` for window-level flags like a failure-rate spike).
        detail: Optional structured context for the JSON output.
    """

    rule: str
    severity: str
    message: str
    event: LedgerEvent | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def flag_could_have_asked(
    events: Iterable[LedgerEvent], config: DigestConfig | None = None
) -> list[Anomaly]:
    """Flag autonomous decisions marked ``could_have_asked=true``.

    These are the calls the live self-report admits the user might have wanted
    to weigh in on — the primary review queue.
    """
    cfg = config if config is not None else DigestConfig()
    out: list[Anomaly] = []
    for e in _ordered(events):
        if e.type != cfg.decision_autonomous_type:
            continue
        if _is_truthy_flag(_data(e).get(cfg.could_have_asked_key)):
            out.append(
                Anomaly(
                    rule="could_have_asked",
                    severity="warn",
                    message=f"Autonomous decision flagged could_have_asked: {_decision_summary(e)}",
                    event=e,
                )
            )
    return out


def flag_failures(
    events: Iterable[LedgerEvent],
    config: DigestConfig | None = None,
    *,
    spike_threshold: int = 3,
) -> list[Anomaly]:
    """Flag each failure in the window, plus a window-level spike when many.

    One ``warn`` per failure event, and one additional ``alert`` for the window
    when the failure count reaches ``spike_threshold`` (a rate spike — the
    window is the rate denominator, set by the caller's ``--since`` / ``--until``).
    """
    cfg = config if config is not None else DigestConfig()
    ordered = _ordered(events)
    failures = [e for e in ordered if e.type == cfg.failure_type]
    out: list[Anomaly] = [
        Anomaly(
            rule="failure",
            severity="warn",
            message=f"Failure: {_failure_summary(e)}",
            event=e,
        )
        for e in failures
    ]
    if len(failures) >= spike_threshold:
        out.append(
            Anomaly(
                rule="failure_spike",
                severity="alert",
                message=f"Failure-rate spike: {len(failures)} failures in window "
                f"(threshold {spike_threshold}).",
                detail={"count": len(failures), "threshold": spike_threshold},
            )
        )
    return out


def flag_push_without_green(
    events: Iterable[LedgerEvent], config: DigestConfig | None = None
) -> list[Anomaly]:
    """Flag a push with an *unresolved* failure before it (best-effort heuristic).

    There is no explicit "tests passed" event in the taxonomy, so "green" is
    inferred from event ordering: a push is suspect when the most recent failure
    before it has **not** been followed by a successful ship (a shipped event
    standing in for a passing/clean marker). Concretely, walking the window in
    time order, we track whether an unresolved failure is outstanding — a
    failure sets the flag, a shipped event clears it — and flag any push that
    fires while the flag is set. Pushes with no preceding failure, or whose
    preceding failure was already cleared by a later ship, are not flagged.
    """
    cfg = config if config is not None else DigestConfig()
    out: list[Anomaly] = []
    unresolved_failure = False
    for e in _ordered(events):
        if e.type == cfg.failure_type:
            unresolved_failure = True
        elif e.type == cfg.shipped_type:
            unresolved_failure = False
        elif e.type == cfg.push_type and unresolved_failure:
            out.append(
                Anomaly(
                    rule="push_without_green",
                    severity="warn",
                    message="Push with an unresolved failure and no clean ship "
                    "since (no green signal before push).",
                    event=e,
                )
            )
    return out


def flag_token_outliers(
    events: Iterable[LedgerEvent], config: DigestConfig | None = None
) -> list[Anomaly]:
    """Flag shipped events whose ``tokens`` / ``duration_s`` are window outliers.

    A metric value is an outlier when it exceeds ``config.outlier_factor`` times
    the median of that metric across shipped events in the window. Needs at
    least two values for a given metric (a single value has no spread to be an
    outlier against). Pure over the window — the median is the window median.
    """
    cfg = config if config is not None else DigestConfig()
    ordered = _ordered(events)
    shipped = [e for e in ordered if e.type == cfg.shipped_type]

    out: list[Anomaly] = []
    for metric_key, label in ((cfg.tokens_key, "tokens"), (cfg.duration_key, "duration_s")):
        pairs: list[tuple[LedgerEvent, float]] = []
        for e in shipped:
            value = _as_number(_data(e).get(metric_key))
            if value is not None:
                pairs.append((e, value))
        if len(pairs) < 2:
            continue
        med = median(n for _e, n in pairs)
        if med <= 0:
            continue
        threshold = med * cfg.outlier_factor
        for e, value in pairs:
            if value > threshold:
                out.append(
                    Anomaly(
                        rule="token_outlier",
                        severity="warn",
                        message=f"Shipped {label} outlier: {value:g} > "
                        f"{cfg.outlier_factor:g}× median ({med:g}).",
                        event=e,
                        detail={
                            "metric": label,
                            "value": value,
                            "median": med,
                            "factor": cfg.outlier_factor,
                        },
                    )
                )
    return out


def flag_refusals(
    events: Iterable[LedgerEvent], config: DigestConfig | None = None
) -> list[Anomaly]:
    """Surface every refusal in the window (what was declined and why)."""
    cfg = config if config is not None else DigestConfig()
    return [
        Anomaly(
            rule="refusal",
            severity="info",
            message=f"Refusal: {_refusal_summary(e)}",
            event=e,
        )
        for e in _ordered(events)
        if e.type == cfg.refusal_type
    ]


def anomalies(
    events: Iterable[LedgerEvent],
    config: DigestConfig | None = None,
    *,
    spike_threshold: int = 3,
) -> list[Anomaly]:
    """Run every anomaly rule and return the union of their flags.

    The input is materialized once and shared across the rules so each rule
    stays pure over an iterable while the caller only walks the stream once.
    """
    cfg = config if config is not None else DigestConfig()
    ordered = _ordered(events)
    out: list[Anomaly] = []
    out.extend(flag_could_have_asked(ordered, cfg))
    out.extend(flag_failures(ordered, cfg, spike_threshold=spike_threshold))
    out.extend(flag_push_without_green(ordered, cfg))
    out.extend(flag_token_outliers(ordered, cfg))
    out.extend(flag_refusals(ordered, cfg))
    return out


# --- the composed digest --------------------------------------------------


@dataclass(frozen=True)
class Digest:
    """The full deterministic oversight digest: rollup + anomaly flags.

    Attributes:
        rollup: The :class:`Rollup` over the window.
        anomalies: The union of every anomaly rule's flags.
    """

    rollup: Rollup
    anomalies: tuple[Anomaly, ...]


def digest(
    events: Iterable[LedgerEvent],
    config: DigestConfig | None = None,
    *,
    spike_threshold: int = 3,
) -> Digest:
    """Compute the full digest (rollup + every anomaly rule) over ``events``.

    Pure over any (one-shot) iterable: materializes the stream once, runs the
    rollup and all anomaly rules against it, and returns a :class:`Digest`.

    Args:
        events: Any iterable of events (typically a window from ``query_events``).
        config: Type/key names to look for (default :class:`DigestConfig`).
        spike_threshold: Failure count that trips the window-level spike alert.

    Returns:
        A :class:`Digest` value object.
    """
    cfg = config if config is not None else DigestConfig()
    ordered = _ordered(events)
    return Digest(
        rollup=rollup(ordered, cfg),
        anomalies=tuple(anomalies(ordered, cfg, spike_threshold=spike_threshold)),
    )
