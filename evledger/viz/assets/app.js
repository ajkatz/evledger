// Ledger viz SPA (viz-frontend-core).
//
// Vanilla JS, no build step, no external deps. Consumes the read-only backend:
//   GET /api/meta   -> { sources:[], types:[], machines:[] }   (filter controls)
//   GET /api/events?source=&type=&machine=&since=&until=&limit=
//                   -> { count, events:[CloudEvents dicts] }
// Each event dict: { specversion, id, source, type, time, datacontenttype,
//                    data?, machine, seq? }.
//
// Views: a filter panel, a canvas timeline (wheel-zoom + drag-pan, lanes by
// source or type, hover tooltip), a sortable data table, and (viz-frontend-flame)
// a zoomable flame graph of the derived spans. The flame graph also consumes:
//   GET /api/spans?<same filters as events>
//                   -> { spans:[{id,base,start,end,seconds,depth,parent,event_ids}],
//                        links:[{from_event_id,to_event_id,kind}] }
//   span.start / span.end are ISO-8601 strings (parsed to ms to share the
//   timeline's data-space view); kind is "pair" or "data:<key>".
//
// Click-to-link (viz-frontend-flame): clicking a timeline dot, a table row, or a
// flame span focuses that event and highlights its related items across ALL
// views — its start↔end pair and data-linked neighbors (one hop over the `links`
// edges, traversed in both directions) plus the matching table row.
//
// Browser-unverified by the build agent (no browser in the sandbox); the parent
// loads it in a real browser to confirm.

"use strict";

(function () {
  // ---- DOM handles -------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const els = {
    status: $("status"),
    liveToggle: $("live-toggle"),
    liveNew: $("live-new"),
    liveFollow: $("live-follow"),
    project: $("f-project"),
    source: $("f-source"),
    type: $("f-type"),
    machine: $("f-machine"),
    since: $("f-since"),
    until: $("f-until"),
    limit: $("f-limit"),
    laneBy: $("f-laneby"),
    apply: $("f-apply"),
    reset: $("f-reset"),
    filters: $("filters"),
    filtersToggle: $("filters-toggle"),
    canvas: $("timeline"),
    tooltip: $("tl-tooltip"),
    fit: $("tl-fit"),
    flame: $("flame"),
    flameTip: $("fl-tooltip"),
    flameFit: $("fl-fit"),
    tbody: $("events-body"),
    table: document.querySelector(".events-table"),
    timelineWrap: document.querySelector(".timeline-wrap"),
    flameWrap: document.querySelector(".flame-wrap"),
  };

  // ---- state -------------------------------------------------------------
  const state = {
    events: [], // raw events from /api/events
    plotted: [], // events with a parsed numeric timestamp (ms), sorted by time
    lanes: [], // lane keys in draw order
    laneIndex: new Map(), // lane key -> row index
    colors: new Map(), // lane key -> color
    // timeline viewport in DATA space (ms): view.start..view.end map to canvas x
    view: { start: 0, end: 1 },
    dataMin: 0,
    dataMax: 1,
    sort: { key: "time", dir: "asc" },
    focusedId: null,
    dpr: 1,
    drag: null, // { lastX } while panning the timeline
    // ---- flame graph + connections (viz-frontend-flame) ----
    spans: [], // /api/spans spans, each augmented with { tStart, tEnd } ms
    links: [], // /api/spans links: { from_event_id, to_event_id, kind }
    adjacency: new Map(), // event id -> Set of directly-linked event ids
    linkedIds: new Set(), // ids related to the focused event (incl. itself)
    eventIdsBySpan: new Map(), // span id -> [event ids] (for span->focus)
    flameDpr: 1,
    flameDrag: null, // { lastX } while panning the flame graph
    // ---- live polling (viz-live) ----
    live: true, // polling on/off (default live)
    follow: false, // auto-follow: keep the view framed on newest events
    seenIds: new Set(), // every event id currently in state.events (dedupe key)
    cursor: null, // newest event `time` seen (ISO string) -> next poll's `since`
    newCount: 0, // events arrived since the last time the view was at the head
    pollTimer: null, // setTimeout handle for the next poll tick
    polling: false, // a poll request is in flight (prevents overlap)
    flashUntil: new Map(), // event id -> wall-clock ms until its pop animation ends
    flashTimer: null, // rAF/timeout handle driving the timeline pop redraws
    loadGen: 0, // bumped on every full load; in-flight polls from an older
    //             filter set are discarded so stale events never merge in.
  };

  // Poll cadence + how long a newly-arrived mark "pops" on the timeline.
  const POLL_INTERVAL_MS = 1500;
  const FLASH_MS = 1600;

  const PAD = { left: 130, right: 16, top: 14, bottom: 26 };
  const LANE_GAP = 4;
  const MIN_LANE_H = 18;

  // Flame graph layout. Shares the timeline's horizontal data-space view; the
  // left gutter is narrower (no lane labels). Rows are stacked by span depth.
  const FLAME_PAD = { left: 12, right: 16, top: 10, bottom: 22 };
  const FLAME_ROW_H = 22; // px per depth level
  const FLAME_ROW_GAP = 2;
  const FLAME_MIN_W = 2; // min px width so zero-duration spans stay visible

  // ---- color palette (deterministic per lane key) ------------------------
  const PALETTE = [
    "#3b6ea5", "#9b59b6", "#27ae60", "#e67e22", "#c0392b",
    "#16a085", "#8e44ad", "#2980b9", "#d35400", "#2c9c6a",
    "#b03a5b", "#7f8c8d", "#cd9b1d", "#5d6d7e", "#1f9e89",
  ];
  function colorFor(key) {
    if (state.colors.has(key)) return state.colors.get(key);
    // FNV-1a hash -> palette index (stable across renders)
    let h = 0x811c9dc5;
    for (let i = 0; i < key.length; i++) {
      h ^= key.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    const color = PALETTE[Math.abs(h) % PALETTE.length];
    state.colors.set(key, color);
    return color;
  }

  // ---- time parsing ------------------------------------------------------
  function parseTime(s) {
    if (typeof s !== "string") return NaN;
    // CloudEvents times are ISO-8601 with a timezone; Date handles them.
    const t = Date.parse(s);
    return Number.isNaN(t) ? NaN : t;
  }

  function fmtTime(ms) {
    if (!Number.isFinite(ms)) return "—";
    const d = new Date(ms);
    // Compact local timestamp; seconds resolution is enough for the axis.
    return d.toLocaleString(undefined, {
      year: "2-digit", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  function formatData(data) {
    // Full, multi-line, untruncated rendering for the table's data cell.
    if (data === undefined || data === null) return "";
    if (typeof data !== "object") return String(data);
    try {
      return JSON.stringify(data, null, 2);
    } catch (e) {
      return String(data);
    }
  }

  // ---- fetching ----------------------------------------------------------
  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  async function loadMeta() {
    try {
      const res = await fetch("/api/meta");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const meta = await res.json();
      fillSelect(els.project, meta.projects || []);
      fillSelect(els.source, meta.sources || []);
      fillSelect(els.type, meta.types || []);
      fillSelect(els.machine, meta.machines || []);
    } catch (err) {
      setStatus("failed to load filter metadata: " + err.message, true);
    }
  }

  function fillSelect(sel, values) {
    // Keep the leading "all" (empty value) option, replace the rest.
    const current = sel.value;
    sel.length = 1;
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    }
    if (current && values.includes(current)) sel.value = current;
  }

  // Shared filter query string for both /api/events and /api/spans. The spans
  // endpoint accepts the same filters as events (minus limit, which is an
  // events-only cap), so the flame graph reflects exactly the filtered set.
  function buildFilterParams() {
    const p = new URLSearchParams();
    if (els.project.value) p.set("project", els.project.value);
    if (els.source.value) p.set("source", els.source.value);
    if (els.type.value) p.set("type", els.type.value);
    if (els.machine.value) p.set("machine", els.machine.value);
    if (els.since.value.trim()) p.set("since", els.since.value.trim());
    if (els.until.value.trim()) p.set("until", els.until.value.trim());
    return p;
  }

  function buildEventsUrl() {
    const p = buildFilterParams();
    const lim = els.limit.value.trim();
    if (lim !== "") p.set("limit", lim);
    const qs = p.toString();
    return qs ? "/api/events?" + qs : "/api/events";
  }

  function buildSpansUrl() {
    const qs = buildFilterParams().toString();
    return qs ? "/api/spans?" + qs : "/api/spans";
  }

  async function loadEvents() {
    // A full load defines a new live baseline; bump the generation so any
    // in-flight poll from the previous filter set is discarded on resolve.
    state.loadGen += 1;
    setStatus("loading events…");
    // Events and spans share the filter state; fetch them together so the
    // timeline/table and the flame graph stay consistent. A spans failure must
    // not blank the events views, so it's handled independently.
    const eventsP = fetch(buildEventsUrl()).then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    });
    const spansP = fetch(buildSpansUrl()).then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    });

    try {
      const body = await eventsP;
      state.events = Array.isArray(body.events) ? body.events : [];
      // A full (re)load is the live baseline: rebuild the dedupe set + cursor
      // from scratch and clear any pending "new" count / flashes.
      resetLiveBaseline();
      const n = state.events.length;
      setStatus(n === 0 ? "no events match the current filters" : n + " event" + (n === 1 ? "" : "s"));
    } catch (err) {
      state.events = [];
      resetLiveBaseline();
      setStatus("failed to load events: " + err.message, true);
    }

    try {
      const body = await spansP;
      onSpansLoaded(body);
    } catch (err) {
      // Spans are best-effort; surface nothing destructive to the events views.
      onSpansLoaded(null);
    }

    onEventsLoaded();

    // Re-arm the poll loop against the fresh baseline (filters may have changed,
    // so a previously-scheduled tick would carry a stale cursor/query).
    schedulePoll();
  }

  // Ingest /api/spans: augment spans with parsed ms bounds, index event->span
  // membership, and build the link adjacency used by click-to-link.
  function onSpansLoaded(body) {
    const spans = body && Array.isArray(body.spans) ? body.spans : [];
    const links = body && Array.isArray(body.links) ? body.links : [];

    state.spans = spans.map((s) => {
      const tStart = parseTime(s.start);
      const tEnd = parseTime(s.end);
      return Object.assign({}, s, {
        tStart: Number.isFinite(tStart) ? tStart : NaN,
        tEnd: Number.isFinite(tEnd) ? tEnd : NaN,
      });
    });

    state.links = links;
    state.eventIdsBySpan = new Map();
    for (const s of state.spans) {
      const ids = Array.isArray(s.event_ids) ? s.event_ids : [];
      state.eventIdsBySpan.set(s.id, ids);
    }

    // Undirected adjacency: each link relates both endpoints regardless of
    // direction, so highlighting reaches an end from its start and vice-versa.
    const adj = new Map();
    const addEdge = (a, b) => {
      if (a == null || b == null) return;
      if (!adj.has(a)) adj.set(a, new Set());
      adj.get(a).add(b);
    };
    for (const link of state.links) {
      addEdge(link.from_event_id, link.to_event_id);
      addEdge(link.to_event_id, link.from_event_id);
    }
    state.adjacency = adj;

    // Recompute the linked set for the current focus against the new edges.
    recomputeLinked();
  }

  // ---- live polling (viz-live) -------------------------------------------
  // Strategy: poll /api/events on an interval using the newest seen event
  // `time` as a `since` cursor (server `since` is inclusive, so the cursor
  // event re-appears — dedupe by id). Genuinely-new events are appended to the
  // in-memory set, the timeline, and the table, flashed in, and the flame graph
  // is re-derived. We never yank the user's view; "follow" opts into recenter.

  // Recompute the dedupe set + cursor from the full event set (called after any
  // full load and after a merge). The cursor is the lexicographically/temporally
  // newest `time` string; ISO-8601 (UTC, fixed-width) sorts correctly as text,
  // but we compare by parsed ms to be safe across offsets.
  function resetLiveBaseline() {
    state.seenIds = new Set();
    state.flashUntil = new Map();
    state.newCount = 0;
    recomputeCursor();
    updateNewIndicator();
  }

  function recomputeCursor() {
    state.seenIds = new Set();
    let maxMs = -Infinity;
    let maxIso = null;
    for (const ev of state.events) {
      if (ev && ev.id != null) state.seenIds.add(ev.id);
      const t = parseTime(ev && ev.time);
      if (Number.isFinite(t) && t >= maxMs) {
        maxMs = t;
        maxIso = ev.time;
      }
    }
    state.cursor = maxIso;
  }

  // (Re)schedule the next poll tick. Clears any pending timer first so we never
  // run two loops; a no-op when paused.
  function schedulePoll() {
    if (state.pollTimer != null) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    if (!state.live) return;
    state.pollTimer = setTimeout(pollTick, POLL_INTERVAL_MS);
  }

  async function pollTick() {
    state.pollTimer = null;
    if (!state.live || state.polling) {
      schedulePoll();
      return;
    }
    state.polling = true;
    const gen = state.loadGen;
    try {
      const fresh = await fetchSince(state.cursor);
      // Discard if a full reload (new filters) happened while we were waiting.
      if (gen === state.loadGen && fresh.length > 0) mergeNewEvents(fresh);
    } catch (err) {
      // A transient poll failure shouldn't kill the loop or blank the views;
      // keep the existing data and try again next tick.
    } finally {
      state.polling = false;
      schedulePoll();
    }
  }

  // Fetch events at/after the cursor under the CURRENT filters, returning only
  // the ones we haven't already seen (dedupe by id covers the inclusive-`since`
  // boundary). Returns [] on any shape/HTTP problem.
  async function fetchSince(cursor) {
    const p = buildFilterParams();
    if (cursor) p.set("since", cursor);
    // Respect the user's limit cap if set (keeps a huge backlog bounded), but
    // the `since` window already keeps the payload small in the common case.
    const lim = els.limit.value.trim();
    if (lim !== "") p.set("limit", lim);
    const qs = p.toString();
    const url = qs ? "/api/events?" + qs : "/api/events";
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const body = await res.json();
    const events = Array.isArray(body.events) ? body.events : [];
    const out = [];
    for (const ev of events) {
      if (ev && ev.id != null && !state.seenIds.has(ev.id)) out.push(ev);
    }
    return out;
  }

  // Append genuinely-new events: extend the in-memory set, mark them for the
  // pop/flash animation, re-derive spans (flame), and re-render — without
  // yanking the timeline unless "follow" is on.
  function mergeNewEvents(fresh) {
    const now = Date.now();
    for (const ev of fresh) {
      state.events.push(ev);
      state.seenIds.add(ev.id);
      state.flashUntil.set(ev.id, now + FLASH_MS);
    }

    recomputeCursor();
    state.newCount += fresh.length;
    updateNewIndicator();

    const n = state.events.length;
    setStatus(n + " event" + (n === 1 ? "" : "s"));

    // The flame graph derives from the full filtered set; re-fetch it so new
    // start/end pairs and links show up. Best-effort: a failure leaves the
    // existing spans in place.
    refetchSpans();

    // Rebuild the plot model (lanes/extent/table) for the enlarged set. This
    // keeps state.view untouched, so the user's pan/zoom is preserved.
    onEventsLoaded(true);

    if (state.follow) followToHead();

    startFlashLoop();
  }

  function refetchSpans() {
    fetch(buildSpansUrl())
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((body) => {
        onSpansLoaded(body);
        drawFlame();
      })
      .catch(() => {
        /* keep existing spans on failure */
      });
  }

  // Slide the view to the newest data while preserving the current zoom (span
  // width). Only used when "follow" is enabled — never forced on the user.
  function followToHead() {
    if (!Number.isFinite(state.dataMax)) return;
    const span = (state.view.end - state.view.start) || 1;
    const margin = span * 0.04;
    state.view.end = state.dataMax + margin;
    state.view.start = state.view.end - span;
    state.newCount = 0;
    updateNewIndicator();
    draw();
    drawFlame();
  }

  function updateNewIndicator() {
    const n = state.newCount;
    if (n > 0 && !state.follow) {
      els.liveNew.hidden = false;
      els.liveNew.textContent = n + " new";
    } else {
      els.liveNew.hidden = true;
    }
  }

  // ---- new-event pop animation (timeline dots) ---------------------------
  // While any mark is within its flash window, redraw the timeline on each
  // frame so the pop ring animates; stop once all flashes expire (so we're not
  // burning rAF when idle).
  function startFlashLoop() {
    if (state.flashTimer != null) return; // already running
    const tick = () => {
      const now = Date.now();
      // Drop expired flashes.
      let active = false;
      for (const [id, until] of state.flashUntil) {
        if (until <= now) state.flashUntil.delete(id);
        else active = true;
      }
      draw();
      if (active) {
        state.flashTimer = window.requestAnimationFrame(tick);
      } else {
        state.flashTimer = null;
      }
    };
    state.flashTimer = window.requestAnimationFrame(tick);
  }

  // 0..1 pop intensity for an id (1 = just arrived, 0 = expired/none).
  function flashIntensity(id, now) {
    const until = state.flashUntil.get(id);
    if (until == null) return 0;
    const remaining = until - now;
    if (remaining <= 0) return 0;
    return Math.max(0, Math.min(1, remaining / FLASH_MS));
  }

  // ---- live toggle / follow wiring ---------------------------------------
  function setLive(on) {
    state.live = !!on;
    els.liveToggle.classList.toggle("is-live", state.live);
    els.liveToggle.setAttribute("aria-pressed", state.live ? "true" : "false");
    els.liveToggle.querySelector(".live-label").textContent =
      state.live ? "live" : "paused";
    if (state.live) {
      // Resuming: poll promptly so the user doesn't wait a full interval to
      // catch up on whatever arrived while paused.
      if (state.pollTimer != null) clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(pollTick, 0);
    } else if (state.pollTimer != null) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function setFollow(on) {
    state.follow = !!on;
    els.liveFollow.checked = state.follow;
    if (state.follow) followToHead(); // snap to head immediately when enabled
    else updateNewIndicator();
  }

  // The user grabbed the view (wheel-zoom or drag-pan): stop auto-following so
  // we don't fight them. New events still arrive + flash; the "N new" badge
  // returns as the cue to jump back to the head.
  function userTookControl() {
    if (state.follow) setFollow(false);
  }

  // ---- derive plot model -------------------------------------------------
  function laneKeyOf(ev) {
    const by = els.laneBy.value === "type" ? "type" : "source";
    return String(ev[by] != null ? ev[by] : "(none)");
  }

  // preserveView=true (live merges) recomputes the plot model + data extent but
  // leaves state.view untouched, so the user's pan/zoom isn't yanked. The full
  // load / lane-change paths pass nothing and re-fit the view to the data.
  function onEventsLoaded(preserveView) {
    // Attach numeric time; keep only parseable events for the timeline.
    state.plotted = state.events
      .map((ev) => ({ ev, t: parseTime(ev.time) }))
      .filter((r) => Number.isFinite(r.t))
      .sort((a, b) => a.t - b.t);

    // Lanes in first-seen order.
    state.lanes = [];
    state.laneIndex = new Map();
    for (const r of state.plotted) {
      const key = laneKeyOf(r.ev);
      if (!state.laneIndex.has(key)) {
        state.laneIndex.set(key, state.lanes.length);
        state.lanes.push(key);
      }
    }

    // Data extent (ms): cover both event marks AND span intervals so the shared
    // view frames the flame graph too. Pad a single-point/empty set so it's valid.
    const times = [];
    for (const r of state.plotted) times.push(r.t);
    for (const s of state.spans) {
      if (Number.isFinite(s.tStart)) times.push(s.tStart);
      if (Number.isFinite(s.tEnd)) times.push(s.tEnd);
    }
    if (times.length === 0) {
      state.dataMin = Date.now() - 60000;
      state.dataMax = Date.now();
    } else {
      state.dataMin = Math.min.apply(null, times);
      state.dataMax = Math.max.apply(null, times);
      if (state.dataMax === state.dataMin) {
        state.dataMin -= 30000;
        state.dataMax += 30000;
      }
    }
    if (preserveView) {
      // Keep the current view; just redraw with the enlarged model.
      draw();
      drawFlame();
    } else {
      fitView(); // also draws both canvases
    }
    renderTable();
    draw();
    drawFlame();
  }

  function fitView() {
    const span = state.dataMax - state.dataMin || 1;
    const margin = span * 0.04;
    state.view.start = state.dataMin - margin;
    state.view.end = state.dataMax + margin;
    draw();
    drawFlame();
  }

  // ---- canvas geometry ---------------------------------------------------
  function plotRect() {
    const w = els.canvas.width / state.dpr;
    const h = els.canvas.height / state.dpr;
    return {
      x: PAD.left,
      y: PAD.top,
      w: Math.max(1, w - PAD.left - PAD.right),
      h: Math.max(1, h - PAD.top - PAD.bottom),
    };
  }

  function timeToX(t, rect) {
    const span = state.view.end - state.view.start || 1;
    return rect.x + ((t - state.view.start) / span) * rect.w;
  }

  function xToTime(x, rect) {
    const span = state.view.end - state.view.start || 1;
    return state.view.start + ((x - rect.x) / rect.w) * span;
  }

  function laneHeight(rect) {
    const n = Math.max(1, state.lanes.length);
    return Math.max(MIN_LANE_H, rect.h / n);
  }

  function laneCenterY(idx, rect) {
    const lh = laneHeight(rect);
    return rect.y + idx * lh + lh / 2;
  }

  // ---- canvas sizing -----------------------------------------------------
  function resizeCanvas() {
    const wrap = els.canvas.parentElement;
    const rect = wrap.getBoundingClientRect();
    const toolbar = els.canvas.previousElementSibling; // .timeline-toolbar
    const toolbarH = toolbar ? toolbar.getBoundingClientRect().height : 0;
    const cssW = Math.max(1, rect.width);
    const cssH = Math.max(80, rect.height - toolbarH);
    state.dpr = window.devicePixelRatio || 1;
    els.canvas.style.height = cssH + "px";
    els.canvas.width = Math.round(cssW * state.dpr);
    els.canvas.height = Math.round(cssH * state.dpr);
    draw();
  }

  // ---- drawing -----------------------------------------------------------
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function draw() {
    const ctx = els.canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    const W = els.canvas.width / state.dpr;
    const H = els.canvas.height / state.dpr;
    ctx.clearRect(0, 0, W, H);

    const rect = plotRect();
    const fg = cssVar("--fg", "#1c1c1e");
    const muted = cssVar("--muted", "#6b6b70");
    const grid = cssVar("--grid", "rgba(0,0,0,0.08)");
    const border = cssVar("--border", "#d8d8de");

    // Lane bands + labels.
    const lh = laneHeight(rect);
    ctx.textBaseline = "middle";
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    for (let i = 0; i < state.lanes.length; i++) {
      const y0 = rect.y + i * lh;
      if (i % 2 === 1) {
        ctx.fillStyle = grid;
        ctx.fillRect(rect.x, y0, rect.w, lh);
      }
      const key = state.lanes[i];
      const cy = y0 + lh / 2;
      // swatch
      ctx.fillStyle = colorFor(key);
      ctx.fillRect(8, cy - 5, 10, 10);
      // label (truncated to the left gutter)
      ctx.fillStyle = fg;
      ctx.textAlign = "left";
      ctx.fillText(truncate(ctx, key, PAD.left - 28), 24, cy);
    }

    // Plot border.
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w - 1, rect.h - 1);

    // Time axis ticks.
    drawTimeAxis(ctx, rect, muted, grid);

    if (state.plotted.length === 0) {
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.font = "13px ui-sans-serif, system-ui, sans-serif";
      ctx.fillText("no events to plot", rect.x + rect.w / 2, rect.y + rect.h / 2);
      return;
    }

    // Event marks (only those visible in the current view). When something is
    // focused, related marks (its pair + data neighbors) get an accent ring and
    // unrelated marks dim, so the connection reads at a glance.
    const accent = cssVar("--accent", "#3b6ea5");
    const hasFocus = state.focusedId != null;
    const r = 3.4;
    const now = Date.now();
    const hasFlash = state.flashUntil.size > 0;
    for (const row of state.plotted) {
      if (row.t < state.view.start || row.t > state.view.end) continue;
      const idx = state.laneIndex.get(laneKeyOf(row.ev));
      if (idx === undefined) continue;
      const x = timeToX(row.t, rect);
      const y = laneCenterY(idx, rect);
      const id = row.ev.id;
      const focused = id === state.focusedId;
      const linked = !focused && state.linkedIds.has(id);
      const dim = hasFocus && !focused && !linked;
      const fill = colorFor(laneKeyOf(row.ev));

      // Pop ring for freshly-arrived marks: an expanding, fading halo. Drawn
      // first (under the dot) so the dot itself stays crisp.
      const pop = hasFlash ? flashIntensity(id, now) : 0;
      if (pop > 0) {
        const ringR = r + 2 + (1 - pop) * 9; // expands as it fades
        ctx.globalAlpha = 0.55 * pop;
        ctx.beginPath();
        ctx.arc(x, y, ringR, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      ctx.globalAlpha = dim ? 0.3 : 1;
      ctx.beginPath();
      const baseR = focused ? r + 2.2 : linked ? r + 1.2 : r;
      ctx.arc(x, y, baseR + pop * 1.6, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();
      if (focused || linked) {
        ctx.lineWidth = focused ? 2 : 1.5;
        ctx.strokeStyle = focused ? fg : accent;
        ctx.stroke();
      } else if (pop > 0) {
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = fg;
        ctx.globalAlpha = pop;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
      ctx.globalAlpha = 1;
    }
    drawNowLine(ctx, rect, timeToX);
  }

  function drawNowLine(ctx, rect, toX) {
    // Vertical reference line at the current wall-clock "now" (advances on each
    // render / poll — i.e. the last-measured now). Skipped when now is outside
    // the current view. `toX` is the caller's time→x projection (timeline or
    // flame), so both canvases share one implementation.
    const now = Date.now();
    // Always show the marker as a reference; clamp to an edge when now is
    // outside the current view (e.g. historical data parked left of "now").
    let x = toX(now, rect);
    if (x < rect.x) x = rect.x;
    else if (x > rect.x + rect.w) x = rect.x + rect.w;
    const accent = cssVar("--accent", "#3b6ea5");
    ctx.save();
    ctx.strokeStyle = accent;
    ctx.globalAlpha = 0.7;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(x + 0.5, rect.y);
    ctx.lineTo(x + 0.5, rect.y + rect.h);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 0.95;
    ctx.fillStyle = accent;
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    const nearRight = x > rect.x + rect.w - 30;
    ctx.textAlign = nearRight ? "right" : "left";
    ctx.fillText("now", nearRight ? x - 3 : x + 3, rect.y + 8);
    ctx.restore();
  }

  function truncate(ctx, text, maxW) {
    if (ctx.measureText(text).width <= maxW) return text;
    let t = text;
    while (t.length > 1 && ctx.measureText(t + "…").width > maxW) {
      t = t.slice(0, -1);
    }
    return t + "…";
  }

  function drawTimeAxis(ctx, rect, muted, grid) {
    const span = state.view.end - state.view.start;
    if (!(span > 0)) return;
    const targetTicks = Math.max(2, Math.floor(rect.w / 120));
    const step = niceStep(span / targetTicks);
    const first = Math.ceil(state.view.start / step) * step;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    for (let t = first; t <= state.view.end; t += step) {
      const x = timeToX(t, rect);
      ctx.strokeStyle = grid;
      ctx.beginPath();
      ctx.moveTo(x, rect.y);
      ctx.lineTo(x, rect.y + rect.h);
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(fmtAxis(t, step), x, rect.y + rect.h + 4);
    }
  }

  // Round a raw ms step up to a "nice" human interval.
  function niceStep(raw) {
    const S = 1000, M = 60 * S, H = 60 * M, D = 24 * H;
    const steps = [
      S, 2 * S, 5 * S, 10 * S, 15 * S, 30 * S,
      M, 2 * M, 5 * M, 10 * M, 15 * M, 30 * M,
      H, 2 * H, 3 * H, 6 * H, 12 * H,
      D, 2 * D, 7 * D, 14 * D, 30 * D, 90 * D, 365 * D,
    ];
    for (const s of steps) if (s >= raw) return s;
    return steps[steps.length - 1];
  }

  function fmtAxis(ms, step) {
    const d = new Date(ms);
    const opts = step < 60000
      ? { hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : step < 24 * 3600000
      ? { hour: "2-digit", minute: "2-digit" }
      : { month: "short", day: "numeric" };
    return d.toLocaleString(undefined, opts);
  }

  // ---- timeline interactions --------------------------------------------
  function canvasPos(evt) {
    const r = els.canvas.getBoundingClientRect();
    return { x: evt.clientX - r.left, y: evt.clientY - r.top };
  }

  // Translate the shared view by a horizontal pixel delta (+dx → forward in
  // time). Used by horizontal wheel/trackpad scroll on either canvas.
  function panView(dxPixels, rect) {
    const span = state.view.end - state.view.start;
    const dt = (dxPixels / (rect.w || 1)) * span;
    state.view.start += dt;
    state.view.end += dt;
  }

  function zoomView(deltaY, anchorX, rect) {
    const anchorT = xToTime(anchorX, rect);
    const factor = Math.pow(1.0015, deltaY); // scroll up (deltaY<0) zooms in
    let newSpan = (state.view.end - state.view.start) * factor;
    const fullSpan = (state.dataMax - state.dataMin) || 1;
    newSpan = Math.max(100, Math.min(fullSpan * 8, newSpan)); // 100ms floor, 8x ceiling
    const frac = (anchorT - state.view.start) / ((state.view.end - state.view.start) || 1);
    state.view.start = anchorT - frac * newSpan;
    state.view.end = state.view.start + newSpan;
  }

  function onWheel(evt) {
    evt.preventDefault();
    userTookControl();
    const rect = plotRect();
    // Horizontal scroll (trackpad swipe / shift+wheel) TRAVERSES the timeline;
    // vertical scroll zooms.
    if (Math.abs(evt.deltaX) > Math.abs(evt.deltaY)) {
      panView(evt.deltaX, rect);
    } else {
      zoomView(evt.deltaY, canvasPos(evt).x, rect);
    }
    draw();
    drawFlame();
  }

  function onPointerDown(evt) {
    els.canvas.setPointerCapture(evt.pointerId);
    state.drag = { lastX: evt.clientX, startX: evt.clientX, moved: false };
    els.canvas.classList.add("dragging");
  }

  function onPointerMove(evt) {
    if (state.drag) {
      const rect = plotRect();
      const span = state.view.end - state.view.start;
      const dxPx = evt.clientX - state.drag.lastX;
      if (Math.abs(evt.clientX - state.drag.startX) > 3) {
        state.drag.moved = true;
        userTookControl();
      }
      const dt = (dxPx / rect.w) * span;
      state.view.start -= dt;
      state.view.end -= dt;
      state.drag.lastX = evt.clientX;
      draw();
      drawFlame();
      return;
    }
    updateTooltip(evt);
  }

  function onPointerUp(evt) {
    if (state.drag) {
      try { els.canvas.releasePointerCapture(evt.pointerId); } catch (e) {}
      const wasDrag = state.drag.moved;
      state.drag = null;
      els.canvas.classList.remove("dragging");
      if (!wasDrag) onTimelineClick(evt); // a tap, not a pan -> select
    }
  }

  // Click on the timeline: a dot under the cursor focuses that event (and its
  // links); empty space clears the highlight.
  function onTimelineClick(evt) {
    const hit = hitTest(canvasPos(evt));
    if (hit) {
      focusEvent(hit.ev.id, false);
    } else {
      clearFocus();
    }
  }

  function hitTest(pos) {
    const rect = plotRect();
    let best = null;
    let bestD = 64; // px^2 threshold (8px radius)
    for (const row of state.plotted) {
      if (row.t < state.view.start || row.t > state.view.end) continue;
      const idx = state.laneIndex.get(laneKeyOf(row.ev));
      if (idx === undefined) continue;
      const x = timeToX(row.t, rect);
      const y = laneCenterY(idx, rect);
      const d = (x - pos.x) * (x - pos.x) + (y - pos.y) * (y - pos.y);
      if (d < bestD) { bestD = d; best = row; }
    }
    return best;
  }

  function updateTooltip(evt) {
    const pos = canvasPos(evt);
    const hit = hitTest(pos);
    if (!hit) {
      els.tooltip.hidden = true;
      return;
    }
    els.tooltip.innerHTML =
      '<div class="tt-type"></div><div class="tt-time"></div>';
    els.tooltip.querySelector(".tt-type").textContent = hit.ev.type || "(no type)";
    els.tooltip.querySelector(".tt-time").textContent = fmtTime(hit.t);
    els.tooltip.hidden = false;
    // Position within the timeline-wrap (tooltip is its child).
    const wrapRect = els.timelineWrap.getBoundingClientRect();
    let left = evt.clientX - wrapRect.left + 12;
    let top = evt.clientY - wrapRect.top + 12;
    const ttW = els.tooltip.offsetWidth;
    if (left + ttW > wrapRect.width) left = wrapRect.width - ttW - 6;
    els.tooltip.style.left = Math.max(0, left) + "px";
    els.tooltip.style.top = top + "px";
  }

  function onPointerLeave() {
    els.tooltip.hidden = true;
  }

  // ---- click-to-link -----------------------------------------------------
  // Compute the set of event ids "related" to the focused event: the event
  // itself plus its 1-hop neighbors over the (undirected) link adjacency — its
  // start↔end pair (kind "pair") and data-linked siblings (kind "data:<key>").
  function recomputeLinked() {
    const linked = new Set();
    if (state.focusedId != null) {
      linked.add(state.focusedId);
      const neighbors = state.adjacency.get(state.focusedId);
      if (neighbors) {
        for (const id of neighbors) linked.add(id);
      }
    }
    state.linkedIds = linked;
  }

  // Reflect the current focus + linked set across every view (timeline marks,
  // table rows, flame spans) without re-querying.
  function applyHighlight() {
    for (const tr of els.tbody.querySelectorAll("tr")) {
      const id = tr.dataset.id;
      tr.classList.toggle("focused", id === state.focusedId);
      tr.classList.toggle(
        "linked",
        id != null && id !== state.focusedId && state.linkedIds.has(id)
      );
    }
    draw();
    drawFlame();
  }

  // Whether a span is highlighted: it's the focused span, or any of its
  // contributing events is in the linked set.
  function spanHighlighted(span) {
    const ids = state.eventIdsBySpan.get(span.id) || [];
    if (state.focusedId != null && ids.indexOf(state.focusedId) !== -1) {
      return "focused";
    }
    for (const id of ids) {
      if (state.linkedIds.has(id)) return "linked";
    }
    return null;
  }

  // Center the timeline on a specific event, focus it, and recompute links.
  function focusEvent(id, recenter) {
    state.focusedId = id;
    recomputeLinked();
    if (recenter !== false) {
      const row = state.plotted.find((r) => r.ev.id === id);
      if (row) {
        const span = state.view.end - state.view.start || 1;
        // Ensure it's comfortably in view; recenter keeping current zoom.
        if (row.t < state.view.start || row.t > state.view.end) {
          state.view.start = row.t - span / 2;
          state.view.end = row.t + span / 2;
        }
      }
    }
    applyHighlight();
  }

  // Clear any current focus/highlight (clicking empty canvas space).
  function clearFocus() {
    if (state.focusedId == null && state.linkedIds.size === 0) return;
    state.focusedId = null;
    state.linkedIds = new Set();
    applyHighlight();
  }

  // ---- flame graph -------------------------------------------------------
  // The flame graph shares the timeline's horizontal data-space view (ms), so
  // zoom/pan in either canvas stays aligned. x = time, width = duration
  // (seconds), rows stacked by span depth, parent gives nesting. Spans render
  // top-down (depth 0 at the top), like a conventional flame/icicle chart.
  function flameRect() {
    const w = els.flame.width / state.flameDpr;
    const h = els.flame.height / state.flameDpr;
    return {
      x: FLAME_PAD.left,
      y: FLAME_PAD.top,
      w: Math.max(1, w - FLAME_PAD.left - FLAME_PAD.right),
      h: Math.max(1, h - FLAME_PAD.top - FLAME_PAD.bottom),
    };
  }

  // Time -> x within the flame plot, using the SHARED timeline view.
  function flameTimeToX(t, rect) {
    const span = state.view.end - state.view.start || 1;
    return rect.x + ((t - state.view.start) / span) * rect.w;
  }

  function flameXToTime(x, rect) {
    const span = state.view.end - state.view.start || 1;
    return state.view.start + ((x - rect.x) / rect.w) * span;
  }

  function flameRowY(depth, rect) {
    return rect.y + depth * (FLAME_ROW_H + FLAME_ROW_GAP);
  }

  // Screen rectangle for a span at the current view, or null if off-screen /
  // unparseable. Zero-duration spans get a FLAME_MIN_W floor so they stay
  // clickable + visible.
  function spanScreenRect(span, rect) {
    if (!Number.isFinite(span.tStart) || !Number.isFinite(span.tEnd)) return null;
    const lo = Math.min(span.tStart, span.tEnd);
    const hi = Math.max(span.tStart, span.tEnd);
    if (hi < state.view.start || lo > state.view.end) return null; // outside view
    let x0 = flameTimeToX(lo, rect);
    let x1 = flameTimeToX(hi, rect);
    let w = x1 - x0;
    if (w < FLAME_MIN_W) w = FLAME_MIN_W;
    const y = flameRowY(Number.isFinite(span.depth) ? span.depth : 0, rect);
    return { x: x0, y: y, w: w, h: FLAME_ROW_H };
  }

  function resizeFlameCanvas() {
    const wrap = els.flame.parentElement;
    const rect = wrap.getBoundingClientRect();
    const toolbar = els.flame.previousElementSibling; // .flame-toolbar
    const toolbarH = toolbar ? toolbar.getBoundingClientRect().height : 0;
    const cssW = Math.max(1, rect.width);
    const cssH = Math.max(80, rect.height - toolbarH);
    state.flameDpr = window.devicePixelRatio || 1;
    els.flame.style.height = cssH + "px";
    els.flame.width = Math.round(cssW * state.flameDpr);
    els.flame.height = Math.round(cssH * state.flameDpr);
    drawFlame();
  }

  function drawFlame() {
    const ctx = els.flame.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(state.flameDpr, 0, 0, state.flameDpr, 0, 0);
    const W = els.flame.width / state.flameDpr;
    const H = els.flame.height / state.flameDpr;
    ctx.clearRect(0, 0, W, H);

    const rect = flameRect();
    const fg = cssVar("--fg", "#1c1c1e");
    const muted = cssVar("--muted", "#6b6b70");
    const grid = cssVar("--grid", "rgba(0,0,0,0.08)");
    const border = cssVar("--border", "#d8d8de");
    const accent = cssVar("--accent", "#3b6ea5");

    // Plot border.
    ctx.strokeStyle = border;
    ctx.lineWidth = 1;
    ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w - 1, rect.h - 1);

    // Shared time axis (same nice-step logic as the timeline).
    drawTimeAxis(ctx, rect, muted, grid);
    drawNowLine(ctx, rect, flameTimeToX);

    if (!state.spans || state.spans.length === 0) {
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = "13px ui-sans-serif, system-ui, sans-serif";
      ctx.fillText(
        "no spans — needs *.start / *.end event pairs",
        rect.x + rect.w / 2,
        rect.y + rect.h / 2
      );
      return;
    }

    const hasFocus = state.focusedId != null;
    ctx.textBaseline = "middle";
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";

    for (const span of state.spans) {
      const r = spanScreenRect(span, rect);
      if (!r) continue;
      // Clip the visible portion to the plot area so wide spans don't overflow.
      const vx0 = Math.max(r.x, rect.x);
      const vx1 = Math.min(r.x + r.w, rect.x + rect.w);
      const vw = vx1 - vx0;
      if (vw <= 0) continue;
      if (r.y > rect.y + rect.h) continue; // below the plot (too deep to show)

      const hl = spanHighlighted(span);
      const dim = hasFocus && !hl;
      ctx.globalAlpha = dim ? 0.3 : 1;

      // Block fill (colored by base type for a stable per-type hue).
      ctx.fillStyle = colorFor(String(span.base || "(span)"));
      ctx.fillRect(vx0, r.y, vw, r.h);

      // Highlight outline for focused / linked spans.
      if (hl) {
        ctx.lineWidth = hl === "focused" ? 2 : 1.5;
        ctx.strokeStyle = hl === "focused" ? fg : accent;
        ctx.strokeRect(vx0 + 0.5, r.y + 0.5, vw - 1, r.h - 1);
      }

      // Label (base + seconds), clipped to the block; skip if too narrow.
      if (vw > 26) {
        const secs = Number.isFinite(span.seconds) ? span.seconds : 0;
        const label = String(span.base || "") + "  " + fmtDuration(secs);
        ctx.save();
        ctx.beginPath();
        ctx.rect(vx0 + 3, r.y, vw - 6, r.h);
        ctx.clip();
        ctx.fillStyle = labelColorOn(span);
        ctx.textAlign = "left";
        ctx.fillText(label, vx0 + 5, r.y + r.h / 2);
        ctx.restore();
      }
      ctx.globalAlpha = 1;
    }
  }

  // Pick a legible label color over the block fill. The palette is mid/dark, so
  // white reads on every hue; keep it simple and consistent.
  function labelColorOn() {
    return "#ffffff";
  }

  function fmtDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    if (seconds < 1) return Math.round(seconds * 1000) + "ms";
    if (seconds < 60) return (Math.round(seconds * 10) / 10) + "s";
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return m + "m" + (s ? " " + s + "s" : "");
    }
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return h + "h" + (m ? " " + m + "m" : "");
  }

  // ---- flame interactions (share the timeline view) ----------------------
  function flamePos(evt) {
    const r = els.flame.getBoundingClientRect();
    return { x: evt.clientX - r.left, y: evt.clientY - r.top };
  }

  function onFlameWheel(evt) {
    evt.preventDefault();
    userTookControl();
    const rect = flameRect();
    // Horizontal scroll traverses the (shared) timeline; vertical zooms.
    if (Math.abs(evt.deltaX) > Math.abs(evt.deltaY)) {
      panView(evt.deltaX, rect);
    } else {
      const anchorT = flameXToTime(flamePos(evt).x, rect);
      const factor = Math.pow(1.0015, evt.deltaY);
      let newSpan = (state.view.end - state.view.start) * factor;
      const fullSpan = (state.dataMax - state.dataMin) || 1;
      newSpan = Math.max(100, Math.min(fullSpan * 8, newSpan));
      const frac = (anchorT - state.view.start) / ((state.view.end - state.view.start) || 1);
      state.view.start = anchorT - frac * newSpan;
      state.view.end = state.view.start + newSpan;
    }
    draw();
    drawFlame();
  }

  function onFlamePointerDown(evt) {
    els.flame.setPointerCapture(evt.pointerId);
    // Track whether this becomes a drag (vs a click) via movement threshold.
    state.flameDrag = { lastX: evt.clientX, startX: evt.clientX, moved: false };
    els.flame.classList.add("dragging");
  }

  function onFlamePointerMove(evt) {
    if (state.flameDrag) {
      const rect = flameRect();
      const span = state.view.end - state.view.start;
      const dxPx = evt.clientX - state.flameDrag.lastX;
      if (Math.abs(evt.clientX - state.flameDrag.startX) > 3) {
        state.flameDrag.moved = true;
        userTookControl();
      }
      const dt = (dxPx / rect.w) * span;
      state.view.start -= dt;
      state.view.end -= dt;
      state.flameDrag.lastX = evt.clientX;
      draw();
      drawFlame();
      return;
    }
    updateFlameTooltip(evt);
  }

  function onFlamePointerUp(evt) {
    if (state.flameDrag) {
      try { els.flame.releasePointerCapture(evt.pointerId); } catch (e) {}
      const wasDrag = state.flameDrag.moved;
      state.flameDrag = null;
      els.flame.classList.remove("dragging");
      if (!wasDrag) onFlameClick(evt); // a tap, not a pan -> select
    }
  }

  function flameHitTest(pos) {
    const rect = flameRect();
    // Iterate in reverse so deeper/later spans (drawn on top) win ties.
    for (let i = state.spans.length - 1; i >= 0; i--) {
      const span = state.spans[i];
      const r = spanScreenRect(span, rect);
      if (!r) continue;
      const vx0 = Math.max(r.x, rect.x);
      const vx1 = Math.min(r.x + r.w, rect.x + rect.w);
      if (
        pos.x >= vx0 && pos.x <= vx1 &&
        pos.y >= r.y && pos.y <= r.y + r.h
      ) {
        return span;
      }
    }
    return null;
  }

  function onFlameClick(evt) {
    const span = flameHitTest(flamePos(evt));
    if (!span) {
      clearFocus();
      return;
    }
    // Focus the span's start event (its id); recompute links from there. Don't
    // recenter the timeline — the span is already framed in the shared view.
    const ids = state.eventIdsBySpan.get(span.id) || [];
    const focusId = ids.length > 0 ? ids[0] : span.id;
    focusEvent(focusId, false);
  }

  function updateFlameTooltip(evt) {
    const pos = flamePos(evt);
    const span = flameHitTest(pos);
    if (!span) {
      els.flameTip.hidden = true;
      return;
    }
    els.flameTip.innerHTML = '<div class="tt-type"></div><div class="tt-time"></div>';
    const secs = Number.isFinite(span.seconds) ? span.seconds : 0;
    els.flameTip.querySelector(".tt-type").textContent =
      String(span.base || "(span)") + " · " + fmtDuration(secs);
    els.flameTip.querySelector(".tt-time").textContent =
      fmtTime(span.tStart) + " → " + fmtTime(span.tEnd);
    els.flameTip.hidden = false;
    const wrapRect = els.flameWrap.getBoundingClientRect();
    let left = evt.clientX - wrapRect.left + 12;
    let top = evt.clientY - wrapRect.top + 12;
    const ttW = els.flameTip.offsetWidth;
    if (left + ttW > wrapRect.width) left = wrapRect.width - ttW - 6;
    els.flameTip.style.left = Math.max(0, left) + "px";
    els.flameTip.style.top = top + "px";
  }

  function onFlamePointerLeave() {
    els.flameTip.hidden = true;
  }

  // ---- table -------------------------------------------------------------
  function sortedEvents() {
    const { key, dir } = state.sort;
    const sign = dir === "desc" ? -1 : 1;
    const copy = state.events.slice();
    copy.sort((a, b) => {
      let av;
      let bv;
      if (key === "time") {
        av = parseTime(a.time);
        bv = parseTime(b.time);
        if (!Number.isFinite(av)) av = 0;
        if (!Number.isFinite(bv)) bv = 0;
      } else if (key === "seq") {
        av = typeof a.seq === "number" ? a.seq : -Infinity;
        bv = typeof b.seq === "number" ? b.seq : -Infinity;
      } else {
        av = String(a[key] != null ? a[key] : "");
        bv = String(b[key] != null ? b[key] : "");
        return sign * av.localeCompare(bv);
      }
      return sign * (av - bv);
    });
    return copy;
  }

  function renderTable() {
    const rows = sortedEvents();
    els.tbody.textContent = "";
    if (rows.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.className = "empty";
      td.textContent = "no events";
      tr.appendChild(td);
      els.tbody.appendChild(tr);
      updateSortIndicators();
      return;
    }
    const frag = document.createDocumentFragment();
    for (const ev of rows) {
      const tr = document.createElement("tr");
      tr.dataset.id = ev.id;
      if (ev.id === state.focusedId) tr.classList.add("focused");
      else if (state.linkedIds.has(ev.id)) tr.classList.add("linked");
      // Flash freshly-arrived rows in (viz-live). The CSS animation is one-shot;
      // the class is harmless once it finishes, and the row is rebuilt on the
      // next render anyway.
      if (state.flashUntil.has(ev.id)) tr.classList.add("just-arrived");

      const tdTime = document.createElement("td");
      tdTime.className = "mono";
      tdTime.textContent = fmtTime(parseTime(ev.time));

      const tdSource = document.createElement("td");
      const sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = colorFor(String(ev.source));
      tdSource.appendChild(sw);
      tdSource.appendChild(document.createTextNode(ev.source != null ? ev.source : ""));

      const tdType = document.createElement("td");
      tdType.textContent = ev.type != null ? ev.type : "";

      const tdSubject = document.createElement("td");
      tdSubject.className = "subject-cell";
      tdSubject.textContent = ev.subject != null ? ev.subject : "";

      const tdMachine = document.createElement("td");
      tdMachine.textContent = ev.machine != null ? ev.machine : "";

      const tdSeq = document.createElement("td");
      tdSeq.className = "num";
      tdSeq.textContent = typeof ev.seq === "number" ? String(ev.seq) : "";

      const tdData = document.createElement("td");
      tdData.className = "data-cell";
      tdData.textContent = formatData(ev.data);

      tr.append(tdTime, tdSource, tdType, tdSubject, tdMachine, tdSeq, tdData);
      tr.addEventListener("click", () => focusEvent(ev.id));
      frag.appendChild(tr);
    }
    els.tbody.appendChild(frag);
    updateSortIndicators();
  }

  function updateSortIndicators() {
    for (const th of els.table.querySelectorAll("th.sortable")) {
      const existing = th.querySelector(".arrow");
      if (existing) existing.remove();
      if (th.dataset.sort === state.sort.key) {
        const arrow = document.createElement("span");
        arrow.className = "arrow";
        arrow.textContent = state.sort.dir === "asc" ? "▲" : "▼";
        th.appendChild(arrow);
      }
    }
  }

  function onHeaderClick(evt) {
    const th = evt.target.closest("th.sortable");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sort.key === key) {
      state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
    } else {
      state.sort.key = key;
      state.sort.dir = "asc";
    }
    renderTable();
  }

  // ---- wiring ------------------------------------------------------------
  function applyFilters() {
    loadEvents();
  }

  function resetFilters() {
    els.project.value = "";
    els.source.value = "";
    els.type.value = "";
    els.machine.value = "";
    els.since.value = "";
    els.until.value = "";
    els.limit.value = "";
    loadEvents();
  }

  function bind() {
    els.apply.addEventListener("click", applyFilters);
    els.reset.addEventListener("click", resetFilters);
    els.filtersToggle.addEventListener("click", () => {
      const collapsed = els.filters.classList.toggle("collapsed");
      els.filtersToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    els.fit.addEventListener("click", fitView);

    // Live controls (viz-live).
    els.liveToggle.addEventListener("click", () => setLive(!state.live));
    els.liveFollow.addEventListener("change", () => setFollow(els.liveFollow.checked));
    // "N new" jumps to the newest events and clears the counter.
    els.liveNew.addEventListener("click", () => {
      followToHead(); // recenters on head + resets newCount/indicator
    });

    // Dropdowns re-query immediately; text/number inputs apply on Enter.
    for (const sel of [els.project, els.source, els.type, els.machine]) {
      sel.addEventListener("change", applyFilters);
    }
    // Re-lane without re-querying.
    els.laneBy.addEventListener("change", () => { onEventsLoaded(); });
    for (const inp of [els.since, els.until, els.limit]) {
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") applyFilters();
      });
    }

    els.table.querySelector("thead").addEventListener("click", onHeaderClick);

    // Timeline gestures. Wheel is non-passive so preventDefault works.
    els.canvas.addEventListener("wheel", onWheel, { passive: false });
    els.canvas.addEventListener("pointerdown", onPointerDown);
    els.canvas.addEventListener("pointermove", onPointerMove);
    els.canvas.addEventListener("pointerup", onPointerUp);
    els.canvas.addEventListener("pointercancel", onPointerUp);
    els.canvas.addEventListener("pointerleave", onPointerLeave);

    // Flame graph gestures (share the timeline's view; click selects a span).
    els.flameFit.addEventListener("click", fitView);
    els.flame.addEventListener("wheel", onFlameWheel, { passive: false });
    els.flame.addEventListener("pointerdown", onFlamePointerDown);
    els.flame.addEventListener("pointermove", onFlamePointerMove);
    els.flame.addEventListener("pointerup", onFlamePointerUp);
    els.flame.addEventListener("pointercancel", onFlamePointerUp);
    els.flame.addEventListener("pointerleave", onFlamePointerLeave);

    window.addEventListener("resize", onResize);
  }

  function onResize() {
    resizeCanvas();
    resizeFlameCanvas();
  }

  // ---- boot --------------------------------------------------------------
  function boot() {
    bind();
    resizeCanvas();
    resizeFlameCanvas();
    // Meta and events load in parallel; loadEvents also fetches /api/spans and
    // drives the first render of the timeline, table, and flame graph.
    loadMeta();
    loadEvents();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
