"""Read-only local web server for the ledger visualizer (``serve-backend``).

This module is the **backend** of the ledger visualizer (the ``serve-backend``
task of the ``ledger-viz`` whim). It serves a single-page app (static assets)
plus three JSON APIs over the ledger, all backed by the public
:mod:`evledger` query/stats API and the pure
:mod:`evledger.viz.connections` derivation. It is strictly
**read-only**: it opens the ledger for reading and never writes it.

The routing/response logic is factored into a single **pure** function,
:func:`handle`, that maps ``(path, query, root)`` to
``(status, content_type, body)`` with no socket involved — so the entire
contract is unit-testable without binding a port. :class:`LedgerVizHandler`
is a thin :class:`~http.server.BaseHTTPRequestHandler` that delegates to it,
and :func:`serve` wires up a :class:`~http.server.ThreadingHTTPServer` bound to
localhost (with a best-effort browser open).

Routes
------
* ``GET /`` → the SPA ``index.html`` (``text/html``).
* ``GET /static/<asset>`` → a static asset from the bundled :mod:`assets`
  directory (``app.js`` / ``app.css`` / etc.), content type by extension.
* ``GET /api/events?source=&project=&type=&machine=&since=&until=&limit=`` →
  ``{"count": N, "events": [<CloudEvents dict>, ...]}`` via
  :func:`~evledger.query_events`. ``project`` is a viz-level grouping over
  ``source`` (the leading token, case-folded), applied as a post-filter.
* ``GET /api/spans?<same filters>`` → ``{"spans": [...], "links": [...]}`` from
  :func:`~evledger.viz.connections.derive_connections` (frozen
  dataclasses rendered to dicts, tuple fields rendered as JSON arrays).
* ``GET /api/meta`` →
  ``{"sources": [...], "projects": [...], "types": [...], "machines": [...]}``
  — the sorted distinct values present in the ledger, for filter controls.

Unknown paths return ``404`` and non-``GET`` methods return ``405``; both as a
small JSON error body.
"""

from __future__ import annotations

import json
import re
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from evledger import LedgerStore, Query, query_events
from evledger.viz.connections import derive_connections

__all__ = ["handle", "LedgerVizHandler", "make_server", "serve", "ASSETS_DIR"]

#: Directory holding the bundled static assets (``index.html``, ``static/*``).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

#: Extension → ``Content-Type`` for static asset serving.
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".map": "application/json; charset=utf-8",
}

_JSON_CT = "application/json; charset=utf-8"


def _single(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first non-empty value for ``key`` in a parsed query, else None.

    ``parse_qs`` yields ``{key: [values...]}``; filter controls send a single
    value per key, and an explicitly blank ``?type=`` should mean "no filter"
    rather than "match the empty string", so blanks collapse to ``None``.
    """
    values = query.get(key)
    if not values:
        return None
    value = values[0]
    return value if value.strip() else None


def _project_of(source: str) -> str:
    """Derive a coarse *project* key from an event ``source``.

    Events from one logical project arrive under several source strings — the
    repo basename varies by working directory (``festcal`` / ``festcal-service``
    / ``FestCal``), and an explicit emit may set its own. We group them by the
    leading alphanumeric token, case-folded: all three festcal sources collapse
    to ``"festcal"``. Purely derived — there is no per-project table to keep in
    sync. A source with no alphanumeric content yields ``""`` (grouping such
    sources together rather than erroring).
    """
    tokens = re.findall(r"[a-z0-9]+", source.lower())
    return tokens[0] if tokens else ""


def _filter_by_project(events: list[Any], project: str | None) -> list[Any]:
    """Keep only events whose derived project matches ``project`` (no-op if None).

    Project is a viz-level grouping over the generic ``source`` field, so it is
    applied here as a post-filter rather than pushed into the core
    :class:`Query` (which the reusable ledger owns and keeps project-agnostic).
    """
    if project is None:
        return events
    return [e for e in events if _project_of(e.source) == project]


def _query_from_params(query: dict[str, list[str]]) -> Query:
    """Build a :class:`Query` from the parsed query-string parameters."""
    return Query(
        source=_single(query, "source"),
        type=_single(query, "type"),
        machine=_single(query, "machine"),
        since=_single(query, "since"),
        until=_single(query, "until"),
    )


def _limit_from_params(query: dict[str, list[str]]) -> int | None:
    """Parse the optional ``limit`` parameter (non-negative int), else None.

    A missing, blank, non-integer, or negative ``limit`` yields ``None`` (no
    cap) — the endpoint is read-only and a bad client value should degrade to
    "everything" rather than error.
    """
    raw = _single(query, "limit")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _apply_limit(events: list[Any], limit: int | None) -> list[Any]:
    """Apply the most-recent-last limit, matching the ``ledger show`` semantics."""
    if limit is None:
        return events
    if limit == 0:
        return []
    return events[-limit:]


def _events_payload(root: Path, query: dict[str, list[str]]) -> dict[str, Any]:
    """Build the ``/api/events`` payload for the given filters."""
    store = LedgerStore(root=root)
    events = query_events(store.iter_events(), _query_from_params(query))
    events = _filter_by_project(events, _single(query, "project"))
    events = _apply_limit(events, _limit_from_params(query))
    dicts = [e.to_dict() for e in events]
    return {"count": len(dicts), "events": dicts}


def _spans_payload(root: Path, query: dict[str, list[str]]) -> dict[str, Any]:
    """Build the ``/api/spans`` payload (derived spans + links) for the filters.

    The filters select the event set first; spans/links are derived only from
    that selection. Frozen dataclasses are rendered via :func:`dataclasses.asdict`,
    which converts their tuple fields (``event_ids``, the span/link tuples) into
    JSON arrays.
    """
    store = LedgerStore(root=root)
    events = query_events(store.iter_events(), _query_from_params(query))
    events = _filter_by_project(events, _single(query, "project"))
    connections = derive_connections(events)
    return {
        "spans": [asdict(span) for span in connections.spans],
        "links": [asdict(link) for link in connections.links],
    }


def _meta_payload(root: Path) -> dict[str, Any]:
    """Build the ``/api/meta`` payload: sorted distinct sources/types/machines."""
    store = LedgerStore(root=root)
    sources: set[str] = set()
    projects: set[str] = set()
    types: set[str] = set()
    machines: set[str] = set()
    for event in store.iter_events():
        sources.add(event.source)
        projects.add(_project_of(event.source))
        types.add(event.type)
        machines.add(event.machine)
    return {
        "sources": sorted(sources),
        "projects": sorted(projects),
        "types": sorted(types),
        "machines": sorted(machines),
    }


def _read_asset(name: str) -> tuple[int, str, bytes] | None:
    """Read a bundled static asset by file name, guarding against traversal.

    Returns ``(status, content_type, body)`` for a readable regular file inside
    :data:`ASSETS_DIR`, or ``None`` when the name escapes the assets directory
    or no such file exists (the caller turns ``None`` into a ``404``).
    """
    # Reject anything that could escape the assets dir before touching the FS.
    if not name or name.startswith("/") or ".." in Path(name).parts:
        return None
    candidate = (ASSETS_DIR / name).resolve()
    try:
        candidate.relative_to(ASSETS_DIR)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    content_type = _CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
    return (200, content_type, candidate.read_bytes())


def _json_body(payload: Any) -> bytes:
    """Serialize a payload to a UTF-8 JSON body."""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def handle(
    path: str,
    query: dict[str, list[str]],
    root: Path,
) -> tuple[int, str, bytes]:
    """Route a ``GET`` request to its ``(status, content_type, body)`` response.

    This is the **pure** core of the server: no socket, no global state, no
    ledger writes. ``path`` is the URL path (no query string), ``query`` is the
    already-parsed query mapping (as from :func:`urllib.parse.parse_qs`), and
    ``root`` is the resolved ledger instance root. It is exercised directly by
    the unit tests; :class:`LedgerVizHandler` is only a transport adapter.

    Args:
        path: The request path, e.g. ``"/api/events"`` (no query string).
        query: The parsed query parameters (``{key: [values]}``).
        root: The resolved ledger instance root directory.

    Returns:
        A ``(status_code, content_type, body_bytes)`` triple.
    """
    if path in ("/", "/index.html"):
        asset = _read_asset("index.html")
        if asset is None:
            return (404, _JSON_CT, _json_body({"error": "index.html not found"}))
        return asset

    if path.startswith("/static/"):
        asset = _read_asset(path[len("/static/"):])
        if asset is None:
            return (404, _JSON_CT, _json_body({"error": "asset not found"}))
        return asset

    if path == "/api/events":
        return (200, _JSON_CT, _json_body(_events_payload(root, query)))

    if path == "/api/spans":
        return (200, _JSON_CT, _json_body(_spans_payload(root, query)))

    if path == "/api/meta":
        return (200, _JSON_CT, _json_body(_meta_payload(root)))

    return (404, _JSON_CT, _json_body({"error": f"not found: {path}"}))


class LedgerVizHandler(BaseHTTPRequestHandler):
    """A thin :class:`BaseHTTPRequestHandler` delegating ``GET`` to :func:`handle`.

    The resolved ledger ``root`` is attached to the server object by
    :func:`make_server` and read back here; the handler holds no other state.
    Only ``GET`` is supported (the server is read-only); every other method
    returns ``405``.
    """

    server_version = "ClaudeKGLedgerViz/0.1"

    # Silence the default stderr request logging; the CLI prints its own banner.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def _root(self) -> Path:
        root: Path = self.server.ledger_root  # type: ignore[attr-defined]
        return root

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        status, content_type, body = handle(parts.path, query, self._root)
        self._respond(status, content_type, body)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        body = _json_body({"error": "method not allowed (read-only server)"})
        self.send_response(405)
        self.send_header("Content-Type", _JSON_CT)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Create (but do not serve) a :class:`ThreadingHTTPServer` for the ledger.

    The resolved ledger ``root`` is stashed on the server instance so the
    stateless :class:`LedgerVizHandler` can read it. Passing ``port == 0`` lets
    the OS pick a free port (useful for hermetic end-to-end tests); the chosen
    port is then readable via ``server.server_address[1]``.

    Args:
        root: The resolved ledger instance root.
        host: The interface to bind (callers pass a localhost address).
        port: The TCP port, or ``0`` to let the OS choose.

    Returns:
        A bound, not-yet-serving :class:`ThreadingHTTPServer`.
    """
    server = ThreadingHTTPServer((host, port), LedgerVizHandler)
    server.ledger_root = root  # type: ignore[attr-defined]
    return server


def serve(
    root: Path,
    port: int = 8765,
    host: str = "127.0.0.1",
    open_browser: bool = True,
) -> None:
    """Start the read-only ledger viz server and block serving requests.

    Binds ``host:port`` (localhost by default), best-effort opens the SPA in a
    browser unless ``open_browser`` is false, and serves until interrupted
    (Ctrl-C). The browser open failure is swallowed — a headless or
    no-default-browser environment must not crash the server.

    Args:
        root: The resolved ledger instance root (read-only).
        port: The TCP port to bind (default ``8765``).
        host: The interface to bind (default ``127.0.0.1``).
        open_browser: Whether to attempt opening the SPA in a browser.
    """
    server = make_server(root, host, port)
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    print(f"ledger viz serving {root} at {url}  (Ctrl-C to stop)")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — best effort; never block serving.
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()
