"""Unit + light end-to-end tests for :mod:`evledger.viz.server`.

The server's routing/response contract lives in the **pure** :func:`handle`
function — ``(path, query, root) -> (status, content_type, body)`` — so the
bulk of these tests drive it directly with no socket. A single hermetic
end-to-end test binds a :class:`ThreadingHTTPServer` on an OS-chosen port in a
background thread to prove the transport adapter (``do_GET``) and static asset
serving line up with :func:`handle`.

Events are written through the **real** :class:`~evledger.LedgerStore`
into a ``tmp_path`` root (no mocks of the stateful store), then read back via
the server — matching the "real implementations against fakes for stateful
stores" rule.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from evledger import LedgerEvent, LedgerStore, new_event
from evledger.viz.server import ASSETS_DIR, _project_of, handle, make_server


def _event(
    *,
    type: str,
    time: str,
    id: str,
    machine: str = "laptop",
    source: str | None = None,
    data: object | None = None,
) -> LedgerEvent:
    """Build an event with explicit id/time/source for determinism."""
    return new_event(
        machine=machine,
        type=type,
        source=source if source is not None else f"/{machine}/sys",
        data=data,
        id=id,
        time=time,
    )


@pytest.fixture
def ledger_root(tmp_path: Path) -> Path:
    """A populated ledger root: a mission start/end pair sharing a whim slug.

    Two machines, two event types, a shared ``data`` key (``whim``) so the spans
    and links derivations both have something to chew on.
    """
    root = tmp_path / "ledger"
    store = LedgerStore(root=root)
    store.append(
        _event(
            type="dev.claude.mission.start",
            time="2026-05-30T00:00:00Z",
            id="a",
            machine="laptop",
            data={"whim": "ledger-viz"},
        )
    )
    store.append(
        _event(
            type="dev.claude.mission.end",
            time="2026-05-30T00:05:00Z",
            id="b",
            machine="laptop",
            data={"whim": "ledger-viz"},
        )
    )
    store.append(
        _event(
            type="dev.claude.note.added",
            time="2026-05-30T00:02:00Z",
            id="c",
            machine="desktop",
            source="/desktop/notes",
        )
    )
    return root


def _json(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


# --- /api/events ----------------------------------------------------------


def test_events_returns_all_events_with_count(ledger_root: Path) -> None:
    status, content_type, body = handle("/api/events", {}, ledger_root)

    assert status == 200
    assert content_type.startswith("application/json")
    payload = _json(body)
    assert payload["count"] == 3
    assert {e["id"] for e in payload["events"]} == {"a", "b", "c"}
    # Ordered by (time, seq): a (00:00) < c (00:02) < b (00:05).
    assert [e["id"] for e in payload["events"]] == ["a", "c", "b"]


def test_events_filters_by_type_glob(ledger_root: Path) -> None:
    status, _ct, body = handle(
        "/api/events", {"type": ["dev.claude.mission.*"]}, ledger_root
    )

    assert status == 200
    payload = _json(body)
    assert payload["count"] == 2
    assert {e["id"] for e in payload["events"]} == {"a", "b"}


def test_events_filters_by_machine(ledger_root: Path) -> None:
    _status, _ct, body = handle(
        "/api/events", {"machine": ["desktop"]}, ledger_root
    )

    payload = _json(body)
    assert payload["count"] == 1
    assert payload["events"][0]["id"] == "c"


def test_events_blank_filter_means_no_filter(ledger_root: Path) -> None:
    _status, _ct, body = handle("/api/events", {"type": [""]}, ledger_root)

    # A blank ?type= collapses to None (no filter), so all three come back.
    assert _json(body)["count"] == 3


def test_events_limit_keeps_most_recent(ledger_root: Path) -> None:
    _status, _ct, body = handle("/api/events", {"limit": ["2"]}, ledger_root)

    payload = _json(body)
    assert payload["count"] == 2
    # Most-recent-last semantics: the last two in (time, seq) order are c, b.
    assert [e["id"] for e in payload["events"]] == ["c", "b"]


def test_events_limit_zero_returns_empty(ledger_root: Path) -> None:
    _status, _ct, body = handle("/api/events", {"limit": ["0"]}, ledger_root)

    payload = _json(body)
    assert payload["count"] == 0
    assert payload["events"] == []


def test_events_bad_limit_is_ignored(ledger_root: Path) -> None:
    _status, _ct, body = handle("/api/events", {"limit": ["nope"]}, ledger_root)

    # Non-integer limit degrades to no cap rather than erroring.
    assert _json(body)["count"] == 3


# --- project grouping (over the source field) -----------------------------


@pytest.mark.parametrize(
    "source, project",
    [
        ("festcal", "festcal"),
        ("festcal-service", "festcal"),  # repo basename varies by cwd...
        ("FestCal", "festcal"),  # ...and case folds together
        ("music-visualizer", "music"),
        ("claude-config", "claude"),
        ("/laptop/sys", "laptop"),  # path-style sources group on first token
        ("", ""),  # no alphanumeric content -> empty group, not an error
    ],
)
def test_project_of_groups_source_variants(source: str, project: str) -> None:
    assert _project_of(source) == project


@pytest.fixture
def festcal_root(tmp_path: Path) -> Path:
    """A ledger whose festcal work is fragmented across three source strings.

    Mirrors the real ledger: the same project surfaces as ``festcal`` (explicit
    emit), ``festcal-service`` (backend repo basename), and ``FestCal`` (Android
    repo basename), alongside an unrelated ``evledger`` event.
    """
    root = tmp_path / "ledger"
    store = LedgerStore(root=root)
    for i, src in enumerate(["festcal", "festcal-service", "FestCal", "evledger"]):
        store.append(
            _event(
                type="dev.claude.task.shipped",
                time=f"2026-06-02T00:0{i}:00Z",
                id=src,
                source=src,
            )
        )
    return root


def test_events_filters_by_project(festcal_root: Path) -> None:
    _status, _ct, body = handle(
        "/api/events", {"project": ["festcal"]}, festcal_root
    )

    payload = _json(body)
    # All three festcal source variants, and only those (evledger excluded).
    assert payload["count"] == 3
    assert {e["id"] for e in payload["events"]} == {
        "festcal",
        "festcal-service",
        "FestCal",
    }


def test_meta_projects_collapse_festcal_variants(festcal_root: Path) -> None:
    _status, _ct, body = handle("/api/meta", {}, festcal_root)

    payload = _json(body)
    # Four distinct sources collapse to two projects.
    assert len(payload["sources"]) == 4
    assert payload["projects"] == ["evledger", "festcal"]


def test_spans_respect_project_filter(festcal_root: Path) -> None:
    # Project filter applies to spans too (same selection feeds the flame graph).
    _status, _ct, body = handle(
        "/api/spans", {"project": ["evledger"]}, festcal_root
    )
    payload = _json(body)
    # Only the lone evledger event survives -> no start/end pair -> no spans.
    assert set(payload) == {"spans", "links"}
    assert payload["spans"] == []


# --- /api/spans -----------------------------------------------------------


def test_spans_renders_dataclasses_as_dicts(ledger_root: Path) -> None:
    status, content_type, body = handle("/api/spans", {}, ledger_root)

    assert status == 200
    assert content_type.startswith("application/json")
    payload = _json(body)
    assert set(payload) == {"spans", "links"}

    # One start/end pair -> exactly one span.
    assert len(payload["spans"]) == 1
    span = payload["spans"][0]
    assert span["base"] == "dev.claude.mission"
    assert span["depth"] == 0
    assert span["parent"] is None
    # Frozen-dataclass tuple field round-trips to a JSON array.
    assert span["event_ids"] == ["a", "b"]
    assert isinstance(span["event_ids"], list)


def test_spans_links_include_pair_and_shared_data(ledger_root: Path) -> None:
    _status, _ct, body = handle("/api/spans", {}, ledger_root)
    links = _json(body)["links"]

    kinds = {link["kind"] for link in links}
    # The start/end pairing edge plus the shared whim-slug data edge.
    assert "pair" in kinds
    assert "data:whim" in kinds

    pair = next(link for link in links if link["kind"] == "pair")
    assert pair["from_event_id"] == "a"
    assert pair["to_event_id"] == "b"


def test_spans_respect_filters(ledger_root: Path) -> None:
    # Filtering to only the note event leaves no start/end pair -> no spans.
    _status, _ct, body = handle(
        "/api/spans", {"machine": ["desktop"]}, ledger_root
    )
    payload = _json(body)
    assert payload["spans"] == []


# --- /api/meta ------------------------------------------------------------


def test_meta_returns_sorted_distinct_values(ledger_root: Path) -> None:
    status, _ct, body = handle("/api/meta", {}, ledger_root)

    assert status == 200
    payload = _json(body)
    assert payload["sources"] == ["/desktop/notes", "/laptop/sys"]
    # Project = leading alphanumeric token of source, case-folded.
    assert payload["projects"] == ["desktop", "laptop"]
    assert payload["types"] == [
        "dev.claude.mission.end",
        "dev.claude.mission.start",
        "dev.claude.note.added",
    ]
    assert payload["machines"] == ["desktop", "laptop"]


def test_meta_empty_ledger(tmp_path: Path) -> None:
    # A missing root reads as empty (store tolerates it); meta is all-empty.
    status, _ct, body = handle("/api/meta", {}, tmp_path / "nope")

    assert status == 200
    assert _json(body) == {
        "sources": [],
        "projects": [],
        "types": [],
        "machines": [],
    }


# --- static assets + routing ---------------------------------------------


def test_root_serves_index_html(ledger_root: Path) -> None:
    status, content_type, body = handle("/", {}, ledger_root)

    assert status == 200
    assert content_type.startswith("text/html")
    assert b"<!DOCTYPE html>" in body


def test_static_serves_app_js(ledger_root: Path) -> None:
    status, content_type, body = handle("/static/app.js", {}, ledger_root)

    assert status == 200
    assert content_type.startswith("text/javascript")
    assert b"fetch" in body


def test_static_serves_app_css(ledger_root: Path) -> None:
    status, content_type, _body = handle("/static/app.css", {}, ledger_root)

    assert status == 200
    assert content_type.startswith("text/css")


def test_static_traversal_is_blocked(ledger_root: Path) -> None:
    status, content_type, _body = handle(
        "/static/../server.py", {}, ledger_root
    )

    # Must not escape the assets dir to read the source module.
    assert status == 404
    assert content_type.startswith("application/json")


def test_unknown_path_is_404(ledger_root: Path) -> None:
    status, content_type, body = handle("/api/nope", {}, ledger_root)

    assert status == 404
    assert content_type.startswith("application/json")
    assert "not found" in _json(body)["error"]


def test_handle_never_writes_the_ledger(ledger_root: Path) -> None:
    before = sorted(p.name for p in ledger_root.rglob("*") if p.is_file())

    handle("/api/events", {}, ledger_root)
    handle("/api/spans", {}, ledger_root)
    handle("/api/meta", {}, ledger_root)

    after = sorted(p.name for p in ledger_root.rglob("*") if p.is_file())
    assert before == after


def test_assets_dir_layout_exists() -> None:
    # The frontend task fills these in; the placeholder shell must exist.
    assert (ASSETS_DIR / "index.html").is_file()
    assert (ASSETS_DIR / "app.js").is_file()
    assert (ASSETS_DIR / "app.css").is_file()


# --- hermetic end-to-end through a real socket ----------------------------


def test_end_to_end_get_over_socket(ledger_root: Path) -> None:
    server = make_server(ledger_root, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/meta", timeout=5
        ) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["machines"] == ["desktop", "laptop"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_end_to_end_post_is_405(ledger_root: Path) -> None:
    server = make_server(ledger_root, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/events", body=b"{}")
        resp = conn.getresponse()
        assert resp.status == 405
        assert resp.getheader("Allow") == "GET"
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
