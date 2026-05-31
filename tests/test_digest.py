"""Unit tests for :mod:`evledger.digest` — the deterministic oversight
digest (rollup + rule-based anomaly flags).

The digest layer is *pure* over an iterable of events: it writes nothing, never
mutates the frozen events, and uses no model. Every rule is a plain computation
over the event stream. Tests build synthetic event sets and assert the rollup
totals and each anomaly rule independently.

The digest is taxonomy-free — a :class:`DigestConfig` names the event types and
``data`` keys the rules look for. These tests use the generic default config
(``task.shipped`` / ``decision.autonomous`` / ``failure`` / ``refusal`` /
``push`` and the ``.start`` / ``.end`` suffixes), which is what the rules look
for out of the box.
"""

from __future__ import annotations

from evledger import (
    DigestConfig,
    LedgerEvent,
    anomalies,
    digest,
    flag_could_have_asked,
    flag_failures,
    flag_push_without_green,
    flag_refusals,
    flag_token_outliers,
    new_event,
    rollup,
)


def _event(
    *,
    type: str,
    time: str,
    data: object | None = None,
    source: str = "/m/sys",
    machine: str = "m",
    seq: int | None = None,
    id: str | None = None,
) -> LedgerEvent:
    """Build a deterministic event (unique id derived from time unless given)."""
    ev = new_event(
        machine=machine,
        type=type,
        source=source,
        data=data,
        id=id if id is not None else time.replace(":", "").replace("-", "")[:32].ljust(32, "0"),
        time=time,
    )
    return ev if seq is None else ev.with_seq(seq)


def _shipped(time: str, **metrics: object) -> LedgerEvent:
    data: dict[str, object] = {"whim": "w", "task": "t", "pr": "#1"}
    data.update(metrics)
    return _event(type="task.shipped", time=time, data=data)


# --- rollup ---------------------------------------------------------------


def test_rollup_counts_events_by_type() -> None:
    events = [
        _event(type="task.shipped", time="2026-05-29T10:00:00Z"),
        _event(type="task.shipped", time="2026-05-29T11:00:00Z"),
        _event(type="failure", time="2026-05-29T12:00:00Z"),
    ]

    r = rollup(events)

    assert r.total == 3
    assert r.counts_by_type == {"task.shipped": 2, "failure": 1}


def test_rollup_sums_shipped_resource_metrics() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z", tokens=100, cost=0.5, duration_s=30),
        _shipped("2026-05-29T11:00:00Z", tokens=200, cost=1.5, duration_s=70),
        # shipped event with no metrics contributes to the count but not sums
        _shipped("2026-05-29T12:00:00Z"),
    ]

    res = rollup(events).resources

    assert res.shipped == 3
    assert res.tokens == 300.0
    assert res.cost == 2.0
    assert res.duration_s == 100.0


def test_rollup_pairs_start_end_durations() -> None:
    events = [
        _event(type="dev.x.mission.start", time="2026-05-29T10:00:00Z"),
        _event(type="dev.x.mission.end", time="2026-05-29T10:30:00Z"),
    ]

    d = rollup(events).durations

    assert d.count == 1
    assert d.total_seconds == 1800.0


def test_rollup_keeps_recent_notables_newest_last() -> None:
    cfg = DigestConfig(recent_limit=2)
    events = [
        _event(
            type="decision.autonomous",
            time="2026-05-29T10:00:00Z",
            data={"summary": "first"},
        ),
        _event(
            type="decision.autonomous",
            time="2026-05-29T11:00:00Z",
            data={"summary": "second"},
        ),
        _event(
            type="decision.autonomous",
            time="2026-05-29T12:00:00Z",
            data={"summary": "third"},
        ),
    ]

    recent = rollup(events, cfg).recent_decisions

    # limit=2 keeps the two newest, ordered oldest->newest (newest last)
    assert [n.summary for n in recent] == ["second", "third"]


def test_rollup_on_empty_stream_is_zeroed() -> None:
    r = rollup([])

    assert r.total == 0
    assert r.counts_by_type == {}
    assert r.resources.shipped == 0
    assert r.resources.tokens == 0.0
    assert r.durations.count == 0
    assert r.recent_decisions == ()


# --- could-have-asked rule -----------------------------------------------


def test_flag_could_have_asked_flags_true_only() -> None:
    events = [
        _event(
            type="decision.autonomous",
            time="2026-05-29T10:00:00Z",
            data={"summary": "picked lib A", "could_have_asked": True},
        ),
        _event(
            type="decision.autonomous",
            time="2026-05-29T11:00:00Z",
            data={"summary": "renamed a var", "could_have_asked": False},
        ),
    ]

    flags = flag_could_have_asked(events)

    assert len(flags) == 1
    assert flags[0].rule == "could_have_asked"
    assert flags[0].event is not None
    assert "picked lib A" in flags[0].message


def test_flag_could_have_asked_tolerates_string_true() -> None:
    events = [
        _event(
            type="decision.autonomous",
            time="2026-05-29T10:00:00Z",
            data={"summary": "x", "could_have_asked": "true"},
        ),
    ]

    assert len(flag_could_have_asked(events)) == 1


def test_flag_could_have_asked_ignores_other_types() -> None:
    events = [
        _event(
            type="failure",
            time="2026-05-29T10:00:00Z",
            data={"could_have_asked": True},
        ),
    ]

    assert flag_could_have_asked(events) == []


# --- failure rule ---------------------------------------------------------


def test_flag_failures_flags_each_failure() -> None:
    events = [
        _event(
            type="failure",
            time="2026-05-29T10:00:00Z",
            data={"kind": "test", "summary": "boom"},
        ),
    ]

    flags = flag_failures(events)

    assert len(flags) == 1
    assert flags[0].rule == "failure"
    assert "[test]" in flags[0].message
    assert "boom" in flags[0].message


def test_flag_failures_adds_spike_alert_at_threshold() -> None:
    events = [
        _event(type="failure", time=f"2026-05-29T1{i}:00:00Z", data={"kind": "test", "summary": "x"})
        for i in range(3)
    ]

    flags = flag_failures(events, spike_threshold=3)

    rules = [f.rule for f in flags]
    assert rules.count("failure") == 3
    assert "failure_spike" in rules
    spike = next(f for f in flags if f.rule == "failure_spike")
    assert spike.severity == "alert"
    assert spike.detail == {"count": 3, "threshold": 3}


def test_flag_failures_no_spike_below_threshold() -> None:
    events = [
        _event(type="failure", time="2026-05-29T10:00:00Z", data={"kind": "bug", "summary": "x"}),
        _event(type="failure", time="2026-05-29T11:00:00Z", data={"kind": "bug", "summary": "y"}),
    ]

    flags = flag_failures(events, spike_threshold=3)

    assert [f.rule for f in flags] == ["failure", "failure"]


# --- push-without-green rule ---------------------------------------------


def test_flag_push_without_green_flags_push_after_unresolved_failure() -> None:
    events = [
        _event(type="failure", time="2026-05-29T10:00:00Z", data={"kind": "test", "summary": "x"}),
        _event(type="push", time="2026-05-29T11:00:00Z", data={"branch": "main"}),
    ]

    flags = flag_push_without_green(events)

    assert len(flags) == 1
    assert flags[0].rule == "push_without_green"
    assert flags[0].event is not None
    assert flags[0].event.type == "push"


def test_flag_push_without_green_clears_after_a_ship() -> None:
    events = [
        _event(type="failure", time="2026-05-29T10:00:00Z", data={"kind": "test", "summary": "x"}),
        _shipped("2026-05-29T10:30:00Z"),  # clean ship clears the failure
        _event(type="push", time="2026-05-29T11:00:00Z", data={"branch": "main"}),
    ]

    assert flag_push_without_green(events) == []


def test_flag_push_without_green_ignores_push_with_no_prior_failure() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z"),
        _event(type="push", time="2026-05-29T11:00:00Z", data={"branch": "main"}),
    ]

    assert flag_push_without_green(events) == []


# --- token / duration outlier rule ---------------------------------------


def test_flag_token_outliers_flags_value_above_factor_times_median() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z", tokens=100),
        _shipped("2026-05-29T11:00:00Z", tokens=100),
        _shipped("2026-05-29T12:00:00Z", tokens=1000),  # 10x median of 100
    ]

    flags = flag_token_outliers(events, DigestConfig(outlier_factor=3.0))

    token_flags = [f for f in flags if f.detail.get("metric") == "tokens"]
    assert len(token_flags) == 1
    assert token_flags[0].rule == "token_outlier"
    assert token_flags[0].detail["value"] == 1000.0
    assert token_flags[0].detail["median"] == 100.0


def test_flag_token_outliers_also_checks_duration() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z", duration_s=10),
        _shipped("2026-05-29T11:00:00Z", duration_s=10),
        _shipped("2026-05-29T12:00:00Z", duration_s=500),
    ]

    flags = flag_token_outliers(events, DigestConfig(outlier_factor=3.0))

    duration_flags = [f for f in flags if f.detail.get("metric") == "duration_s"]
    assert len(duration_flags) == 1
    assert duration_flags[0].detail["value"] == 500.0


def test_flag_token_outliers_needs_two_values() -> None:
    events = [_shipped("2026-05-29T10:00:00Z", tokens=1_000_000)]

    assert flag_token_outliers(events) == []


def test_flag_token_outliers_no_flag_when_within_factor() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z", tokens=100),
        _shipped("2026-05-29T11:00:00Z", tokens=200),  # 2x median, under 3x
    ]

    assert flag_token_outliers(events, DigestConfig(outlier_factor=3.0)) == []


# --- refusal rule ---------------------------------------------------------


def test_flag_refusals_surfaces_each_refusal() -> None:
    events = [
        _event(
            type="refusal",
            time="2026-05-29T10:00:00Z",
            data={"action": "force-push to main", "reason": "rewrites history"},
        ),
    ]

    flags = flag_refusals(events)

    assert len(flags) == 1
    assert flags[0].rule == "refusal"
    assert "force-push to main" in flags[0].message
    assert "rewrites history" in flags[0].message


# --- composed digest ------------------------------------------------------


def test_anomalies_unions_every_rule() -> None:
    events = [
        _event(
            type="decision.autonomous",
            time="2026-05-29T09:00:00Z",
            data={"summary": "x", "could_have_asked": True},
        ),
        _event(type="failure", time="2026-05-29T10:00:00Z", data={"kind": "test", "summary": "x"}),
        _event(type="push", time="2026-05-29T10:30:00Z", data={"branch": "main"}),
        # three shipped values so the median (100) is low enough that the
        # 1000-token event trips the >3x outlier rule; a 2-value window would
        # take the mean-of-middle (550) and 1000 < 3x550 would not flag.
        _shipped("2026-05-29T11:00:00Z", tokens=100),
        _shipped("2026-05-29T11:15:00Z", tokens=100),
        _shipped("2026-05-29T11:30:00Z", tokens=1000),
        _event(
            type="refusal",
            time="2026-05-29T12:00:00Z",
            data={"action": "a", "reason": "b"},
        ),
    ]

    rules = {a.rule for a in anomalies(events)}

    assert "could_have_asked" in rules
    assert "failure" in rules
    assert "push_without_green" in rules
    assert "token_outlier" in rules
    assert "refusal" in rules


def test_digest_composes_rollup_and_anomalies() -> None:
    events = [
        _shipped("2026-05-29T10:00:00Z", tokens=100, cost=1.0),
        _event(
            type="refusal",
            time="2026-05-29T11:00:00Z",
            data={"action": "a", "reason": "b"},
        ),
    ]

    report = digest(events)

    assert report.rollup.total == 2
    assert report.rollup.resources.tokens == 100.0
    assert any(a.rule == "refusal" for a in report.anomalies)


def test_digest_is_pure_does_not_consume_a_generator_twice_for_caller() -> None:
    # digest materializes the stream once; passing a one-shot generator works.
    events = (
        e
        for e in [
            _shipped("2026-05-29T10:00:00Z", tokens=100),
            _shipped("2026-05-29T11:00:00Z", tokens=100),
        ]
    )

    report = digest(events)

    assert report.rollup.total == 2
    assert report.rollup.resources.tokens == 200.0
