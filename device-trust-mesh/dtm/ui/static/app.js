// Device Trust Mesh page: a "snapshot" event replaces every piece of state, then "batch" events carry
// lists of {type, data} deltas. EventSource reconnects by itself and the server answers with a snapshot.
"use strict";

(() => {
  // Flink commits arrive in bursts, so a batch's dots are spread over at most this long to keep the graph moving.
  const SPREAD_MS = 10000;
  const DOT_MS = 1400;
  const RESYNC_MS = 1000;
  const FEED_LIMIT = 500;
  const MAX_DOTS = 300;
  const BUBBLE_HOLD_MS = 2000;
  const ROUTER = "dtm-a2a-task-router";
  const FRESHNESS_TIP = "measured at a 500 ms poll interval";
  const MATERIALIZATION_TIP = "Kafka record timestamp → applied in context-mcp; "
    + "mostly the wait for Flink's next checkpoint commit";
  const EMPTY_TEXT = "waiting for traffic — scale device-simulator to 1";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SEED_LAYOUT = new Map([
    [ROUTER, [80, 120]], ["risk", [235, 62]], ["enrolment", [395, 120]],
    ["compliance", [560, 45]], ["step-up", [560, 120]], ["revocation", [560, 195]],
  ]);
  const AGENT_CARDS = [["risk", "Risk"], ["enrolment", "Enrolment"], ["step-up", "Step-up"],
    ["compliance", "Compliance"], ["revocation", "Revocation"]];
  const NODE_LABELS = { [ROUTER]: "router" };
  const STALE_PANES = { kafka: ["pane-traffic", "pane-pairs"], context: ["pane-devices"] };
  const COLORS = [["red", "blocked"], ["amber", "step-up"], ["green", "trusted"], ["grey", "pending"]];

  const $ = (sel, root = document) => root.querySelector(sel);
  const graph = $("#traffic-graph");
  const edgesLayer = $(".edges", graph);
  const dotsLayer = $(".dots", graph);
  const nodesLayer = $(".nodes", graph);
  const emptyText = $(".empty", graph);
  const devicesBody = $("#devices-table tbody");
  const feedBody = $("#feed tbody");
  const pairsBody = $("#pairs-table tbody");
  const trafficPane = $("#pane-traffic");
  const activityCards = $("#activity-cards");

  let state = freshState();
  const deviceRows = new Map();
  // One entry per agent card: {agent, el, slot, tag, shown, shownAt, pending, timer}.
  const cards = new Map();
  let linked = null;
  const edgeEls = new Map();
  const nodeEls = new Map();
  let schedule = [];
  let dots = [];
  let lastMessagesAt = null;

  function freshState() {
    return {
      devices: new Map(), feed: [], feedKeys: new Set(), pairs: { window_end_ms: null, window_ms: null, ranking: [] },
      summary: null, stale: { kafka: null, context: null }, edges: new Set(), extras: [], filter: null,
    };
  }

  // ---- formatting ----
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  function hms(ms) {
    const d = new Date(ms);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  const hmsMillis = (ms) => `${hms(ms)}.${pad(new Date(ms).getMilliseconds(), 3)}`;
  const seconds = (ms) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)} s`);
  const lag = (ms, over) => {
    if (ms == null) return "—";
    if (over) return `> ${Math.round(ms / 1000)} s`;
    return ms >= 1000 ? `~${(ms / 1000).toFixed(1)} s` : `~${Math.round(ms)} ms`;
  };
  const freshness = (ms) => (ms == null ? "—" : `≤ ${Math.round(ms)} ms`);
  const agentLabel = (name) => NODE_LABELS[name] || name || "?";
  const edgeKey = (source, target) => `${source}→${target}`;
  const messageKey = (m) => `${m.topic}|${m.partition}|${m.offset}`;

  function el(tag, attrs = {}, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    return node;
  }

  function restartAnimation(node, cls) {
    node.classList.remove(cls);
    void node.getBoundingClientRect();
    node.classList.add(cls);
  }

  // ---- devices ----
  function upsertDevice(row, live) {
    const previous = state.devices.get(row.device_id);
    state.devices.set(row.device_id, row);
    return live && previous !== undefined && previous.changed_at_ms !== row.changed_at_ms;
  }

  function renderDevices(flash) {
    const ordered = [...state.devices.values()].sort(
      (a, b) => b.changed_at_ms - a.changed_at_ms || a.device_id.localeCompare(b.device_id));
    for (const [id, tr] of deviceRows) {
      if (!state.devices.has(id)) { tr.remove(); deviceRows.delete(id); }
    }
    ordered.forEach((row, i) => {
      let tr = deviceRows.get(row.device_id);
      if (!tr) {
        tr = el("tr", { "data-device-id": row.device_id });
        tr.addEventListener("animationend", () => tr.classList.remove("flash"));
        for (const cls of ["", "", "", "reason", "", "num", "num fresh"]) tr.append(el("td", cls ? { class: cls } : {}));
        tr.cells[6].title = FRESHNESS_TIP;
        tr.cells[2].append(el("span", { class: "pill" }));
        deviceRows.set(row.device_id, tr);
      }
      const kept = ["flash", "linked"].filter((cls) => tr.classList.contains(cls));
      tr.className = [`c-${row.color}`, ...kept].join(" ");
      tr.cells[0].textContent = row.device_id;
      tr.cells[1].textContent = row.customer_id || "—";
      const pill = tr.cells[2].firstChild;
      pill.className = `pill ${row.color}`;
      pill.textContent = row.label;
      tr.cells[3].textContent = row.reason || "—";
      tr.cells[3].title = row.reason || "";
      tr.cells[4].textContent = row.changed_at_ms ? hms(row.changed_at_ms) : "—";
      tr.cells[5].textContent = seconds(row.latency_ms);
      tr.cells[6].textContent = freshness(row.freshness_ms);
      if (devicesBody.children[i] !== tr) devicesBody.insertBefore(tr, devicesBody.children[i] || null);
      if (flash.has(row.device_id)) restartAnimation(tr, "flash");
    });
  }

  function renderSummary() {
    const s = state.summary;
    const box = $("#summary");
    box.replaceChildren();
    if (!s) return;
    const kpi = (value, label, cls, title) => {
      const div = el("div", { class: `kpi${cls ? ` ${cls}` : ""}` });
      if (title) div.title = title;
      div.append(el("b", {}, value), label);
      box.append(div);
    };
    kpi(String(s.device_count), "devices");
    for (const [color, label] of COLORS) kpi(String(s.by_color[color] || 0), label, color);
    kpi(`${seconds(s.decision_p50_ms)} / ${seconds(s.decision_p95_ms)}`, "event → decision p50 / p95");
    kpi(`${freshness(s.freshness_p50_ms)} / ${freshness(s.freshness_p95_ms)}`, "freshness p50 / p95", "",
        FRESHNESS_TIP);
    kpi(`${lag(s.context_lag_p50_ms, s.context_lag_p50_over)} / ${lag(s.context_lag_p95_ms, s.context_lag_p95_over)}`,
        "context materialization p50 / p95", "", MATERIALIZATION_TIP);
  }

  // ---- feed ----
  function addMessage(m) {
    const key = messageKey(m);
    if (state.feedKeys.has(key)) return false;
    state.feedKeys.add(key);
    state.feed.unshift(m);
    while (state.feed.length > FEED_LIMIT) {
      const old = state.feed.pop();
      state.feedKeys.delete(messageKey(old));
      const row = feedBody.querySelector(`tr[data-key="${CSS.escape(messageKey(old))}"]`);
      if (row) row.remove();
    }
    drawNode(m.source);
    drawNode(m.target);
    if (m.kind === "task" && m.source && m.target) addEdge(m.source, m.target);
    return true;
  }

  const matchesFilter = (m) => !state.filter || (m.source === state.filter[0] && m.target === state.filter[1]);

  function feedRow(m) {
    const tr = el("tr", {
      class: "feed-row", "data-key": messageKey(m), "data-topic": m.topic, "data-partition": m.partition,
      "data-offset": m.offset, "data-message-id": m.message_id,
    });
    tr.append(el("td", {}, hmsMillis(m.time_ms)),
              el("td", {}, `${agentLabel(m.source)} → ${agentLabel(m.target)}`),
              el("td", { class: `k-${m.kind}` }, m.kind),
              el("td", {}, m.device_id),
              el("td", { class: "pos" }, `${m.topic} @ ${m.partition}:${m.offset}`));
    return tr;
  }

  function renderFeed() {
    feedBody.replaceChildren(...state.feed.filter(matchesFilter).map(feedRow));
  }

  function prependFeed(messages) {
    for (const m of messages) if (matchesFilter(m)) feedBody.insertBefore(feedRow(m), feedBody.firstChild);
  }

  function setFilter(pair) {
    state.filter = pair;
    const chip = $(".filter", trafficPane);
    chip.hidden = !pair;
    if (pair) $(".filter-pair", chip).textContent = `${agentLabel(pair[0])} → ${agentLabel(pair[1])}`;
    for (const [key, g] of edgeEls) {
      g.classList.toggle("selected", !!pair && key === edgeKey(pair[0], pair[1]));
      g.classList.toggle("dim", !!pair && key !== edgeKey(pair[0], pair[1]));
    }
    renderFeed();
  }

  // ---- graph ----
  function position(agent) {
    if (SEED_LAYOUT.has(agent)) return SEED_LAYOUT.get(agent);
    let i = state.extras.indexOf(agent);
    if (i < 0) i = state.extras.push(agent) - 1;
    return [90 + (i % 5) * 115, 232 - Math.floor(i / 5) * 30];
  }

  function addEdge(source, target) {
    const key = edgeKey(source, target);
    if (state.edges.has(key)) return;
    state.edges.add(key);
    drawNode(source);
    drawNode(target);
    const [x1, y1] = position(source);
    const [x2, y2] = position(target);
    const g = svg("g", { class: "edge" });
    g.append(svg("line", { class: "edge-line", x1, y1, x2, y2, "stroke-width": 1.5 }),
             svg("line", { class: "edge-hit", x1, y1, x2, y2 }),
             svg("text", { class: "edge-rate", x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 7, "text-anchor": "middle" }),
             svg("title", {}, `${agentLabel(source)} → ${agentLabel(target)}: click to filter the feed`));
    g.addEventListener("click", () => setFilter([source, target]));
    if (state.filter) g.classList.add("dim");
    edgeEls.set(key, g);
    edgesLayer.append(g);
    nodeEls.get(source).classList.remove("idle");
    nodeEls.get(target).classList.remove("idle");
  }

  function drawNode(agent) {
    if (!agent || nodeEls.has(agent)) return;
    const [x, y] = position(agent);
    const label = agentLabel(agent);
    const width = label.length * 7.4 + 24;
    const g = svg("g", { class: `node idle${agent === ROUTER ? " flink" : ""}`, transform: `translate(${x} ${y})` });
    g.append(svg("rect", { x: -width / 2, y: -14, width, height: 28, rx: 14 }),
             svg("text", { "text-anchor": "middle", y: 4.5 }, label),
             svg("title", {}, agent === ROUTER ? `${agent} (Flink)` : agent));
    g.addEventListener("animationend", () => g.classList.remove("pulse"));
    nodeEls.set(agent, g);
    nodesLayer.append(g);
  }

  function resetGraph() {
    edgesLayer.replaceChildren();
    nodesLayer.replaceChildren();
    dotsLayer.replaceChildren();
    edgeEls.clear();
    nodeEls.clear();
    schedule = [];
    dots = [];
    lastMessagesAt = null;
    for (const agent of SEED_LAYOUT.keys()) drawNode(agent);
  }

  function renderEdgeWeights() {
    const rates = new Map(state.pairs.ranking.map((r) => [edgeKey(r.from_agent, r.to_agent), r.per_minute]));
    const max = Math.max(1, ...rates.values());
    for (const [key, g] of edgeEls) {
      const rate = rates.get(key) || 0;
      g.firstChild.setAttribute("stroke-width", (1.5 + 5 * rate / max).toFixed(1));
      g.querySelector(".edge-rate").textContent = rate ? `${(rate / 60).toFixed(1)}/s` : "";
    }
  }

  function renderEmpty() {
    emptyText.textContent = EMPTY_TEXT;
    emptyText.toggleAttribute("hidden", state.feed.length > 0);
  }

  // A batch's dots and pulses keep their true order and are spread evenly over the time since the previous
  // batch of messages arrived, capped at SPREAD_MS; the first batch after a snapshot uses SPREAD_MS. One Flink
  // commit every ~10 s gets the full spread, while small frequent batches stay close to their feed rows.
  // Nothing is queued while the tab is hidden, so a returning tab never replays a backlog.
  function scheduleVisuals(messages) {
    const now = performance.now();
    const spread = lastMessagesAt === null ? SPREAD_MS : Math.min(SPREAD_MS, now - lastMessagesAt);
    lastMessagesAt = now;
    if (document.hidden) return;
    const visual = messages.filter((m) => m.kind === "task" || m.kind === "decision")
      .sort((a, b) => a.time_ms - b.time_ms);
    if (!visual.length) return;
    schedule = schedule.filter((ev) => ev.at >= now - SPREAD_MS);
    const gap = spread / visual.length;
    visual.forEach((m, i) => schedule.push({ at: now + i * gap, kind: m.kind, source: m.source, target: m.target }));
    schedule.sort((a, b) => a.at - b.at);
  }

  function fire(ev, now) {
    if (ev.kind === "decision") {
      const node = nodeEls.get(ev.source);
      if (node) restartAnimation(node, "pulse");
      return;
    }
    if (dots.length >= MAX_DOTS || !state.edges.has(edgeKey(ev.source, ev.target))) return;
    const [x1, y1] = position(ev.source);
    const [x2, y2] = position(ev.target);
    const circle = svg("circle", { class: "dot", r: 4.5, cx: x1, cy: y1 });
    dotsLayer.append(circle);
    dots.push({ circle, x1, y1, x2, y2, start: now });
  }

  function frame(now) {
    while (schedule.length && schedule[0].at <= now) {
      const ev = schedule.shift();
      if (now - ev.at < SPREAD_MS) fire(ev, now);
    }
    dots = dots.filter((d) => {
      const t = (now - d.start) / DOT_MS;
      if (t >= 1) { d.circle.remove(); return false; }
      d.circle.setAttribute("cx", (d.x1 + (d.x2 - d.x1) * t).toFixed(1));
      d.circle.setAttribute("cy", (d.y1 + (d.y2 - d.y1) * t).toFixed(1));
      return true;
    });
    requestAnimationFrame(frame);
  }

  // ---- pairs ----
  function sparkline(history) {
    const w = 120, h = 22;
    const max = Math.max(1, ...history);
    const step = history.length > 1 ? w / (history.length - 1) : 0;
    const points = history.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`);
    const s = svg("svg", { class: "spark", width: w, height: h, viewBox: `0 0 ${w} ${h}` });
    s.append(svg("polyline", { points: points.join(" ") }));
    return s;
  }

  function renderPairs() {
    const { ranking, window_end_ms: end, window_ms: length } = state.pairs;
    const average = length == null ? "" : `avg over last ${Math.max(1, Math.round(length / 60000))} min · `;
    $("#pane-pairs .window").textContent = end ? `${average}window ending ${hms(end)}` : "";
    const max = Math.max(1, ...ranking.map((r) => r.per_minute));
    pairsBody.replaceChildren(...ranking.map((r) => {
      const tr = el("tr");
      const bar = el("div", { class: "bar" });
      bar.style.width = `${(100 * r.per_minute / max).toFixed(1)}%`;
      const barCell = el("td", { class: "bar-col" });
      barCell.append(bar);
      const sparkCell = el("td", { class: "spark-col" });
      sparkCell.append(sparkline(r.history || []));
      tr.append(el("td", {}, `${agentLabel(r.from_agent)} → ${agentLabel(r.to_agent)}`),
                el("td", { class: "num" }, r.per_minute.toFixed(1)), barCell, sparkCell);
      return tr;
    }));
  }

  // ---- agent activity ----
  const agentName = (agent) => (AGENT_CARDS.find(([key]) => key === agent) || [agent, agent])[1];

  function cardFor(agent) {
    let card = cards.get(agent);
    if (card) return card;
    const root = el("div", { class: "card", "data-agent": agent });
    const slot = el("div", { class: "bubble-slot" });
    const tag = el("span", { class: "tag" }, "disabled");
    const label = el("div", { class: "card-label" }, `${agentName(agent)} Agent`);
    label.append(tag);
    root.append(slot, label);
    card = { agent, el: root, slot, tag, shown: null, shownAt: 0, pending: null, timer: null };
    cards.set(agent, card);
    activityCards.append(root);
    paintCard(card);
    return card;
  }

  function linkRow(bubble, deviceId) {
    unlinkRow();
    const row = devicesBody.querySelector(`tr[data-device-id="${CSS.escape(deviceId)}"]`);
    if (!row) return;
    row.classList.add("linked");
    row.scrollIntoView({ block: "nearest" });
    linked = { row, bubble };
  }

  function unlinkRow() {
    if (linked) linked.row.classList.remove("linked");
    linked = null;
  }

  function bubbleEl(b) {
    const node = el("div", { class: `bubble b-${b.color}`, "data-message-id": b.message_id });
    const line = (label, value) => {
      const div = el("div", { class: "line" });
      div.append(el("b", {}, label), value);
      return div;
    };
    node.append(line("To:", b.to), line("Customer:", b.customer_id || "—"), line("Device:", b.device_id),
                line("Action:", b.action));
    node.addEventListener("mouseenter", () => linkRow(node, b.device_id));
    node.addEventListener("mouseleave", unlinkRow);
    return node;
  }

  function paintCard(card) {
    if (linked && card.slot.contains(linked.bubble)) unlinkRow();
    const b = card.shown;
    const disabled = !b && card.agent === "revocation";
    card.el.classList.toggle("disabled", disabled);
    card.tag.hidden = !disabled;
    card.slot.replaceChildren(b ? bubbleEl(b) : el("div", { class: "waiting" }, "waiting for activity"));
  }

  function showBubble(card, b) {
    card.shown = b;
    card.shownAt = performance.now();
    paintCard(card);
  }

  // A bubble stays up for at least BUBBLE_HOLD_MS; a newer one arriving sooner waits as the card's single
  // pending bubble, replacing any earlier pending one, and shows when the hold elapses.
  function offerBubble(b) {
    const card = cardFor(b.agent);
    if ((card.shown && card.shown.message_id === b.message_id)
        || (card.pending && card.pending.message_id === b.message_id)) return;
    const remaining = card.shown ? BUBBLE_HOLD_MS - (performance.now() - card.shownAt) : 0;
    if (remaining <= 0) {
      clearTimeout(card.timer);
      card.timer = null;
      card.pending = null;
      showBubble(card, b);
      return;
    }
    card.pending = b;
    if (card.timer === null) {
      card.timer = setTimeout(() => {
        card.timer = null;
        const next = card.pending;
        card.pending = null;
        if (next) showBubble(card, next);
      }, remaining);
    }
  }

  function renderActivity(activity) {
    for (const [agent, card] of cards) {
      clearTimeout(card.timer);
      card.timer = null;
      card.pending = null;
      if (!(agent in activity) && !AGENT_CARDS.some(([key]) => key === agent)) {
        card.el.remove();
        cards.delete(agent);
      }
    }
    for (const [agent] of AGENT_CARDS) cardFor(agent);
    for (const agent of Object.keys(activity)) cardFor(agent);
    for (const [agent, card] of cards) {
      card.shown = activity[agent] || null;
      card.shownAt = performance.now();
      paintCard(card);
    }
  }

  // ---- stale banners ----
  function renderStale() {
    const since = { "pane-devices": [], "pane-traffic": [], "pane-pairs": [] };
    for (const [source, panes] of Object.entries(STALE_PANES)) {
      const at = state.stale[source];
      if (at != null) for (const pane of panes) since[pane].push(at);
    }
    for (const [pane, times] of Object.entries(since)) {
      const banner = $(`#${pane} .stale`);
      banner.hidden = times.length === 0;
      banner.textContent = times.length ? `stale since ${hms(Math.min(...times))}` : "";
    }
  }

  // ---- maximize ----
  const panes = [...document.querySelectorAll(".pane")];

  function setMaximized(target) {
    for (const pane of panes) {
      const on = pane === target;
      pane.classList.toggle("maximized", on);
      const button = $(".maximize", pane);
      button.textContent = on ? "⤡" : "⤢";
      button.setAttribute("aria-label", on ? "Restore" : "Maximize");
    }
    document.body.classList.toggle("has-max", !!target);
  }

  for (const pane of panes) {
    $(".maximize", pane).addEventListener("click", () =>
      setMaximized(pane.classList.contains("maximized") ? null : pane));
  }
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
    setMaximized(null);
  });
  $(".clear-filter", trafficPane).addEventListener("click", () => setFilter(null));

  // ---- message viewer ----
  const viewer = $("#message-viewer");
  const viewerBody = $(".viewer-body", viewer);
  const READABLE_FIELDS = {
    task: ["task_type", "summary"],
    decision: ["outcome", "policy_action", "risk_score", "rationale", "signal_ids", "method"],
    status: ["state", "status_text"],
  };
  let viewerRequest = 0;

  const isEmpty = (v) => v == null || v === "";
  const fieldText = (v) => (typeof v === "object" ? JSON.stringify(v) : String(v));

  function viewerNote(text) {
    viewerBody.replaceChildren(el("p", { class: "viewer-note" }, text));
  }

  function copyJson(text, button) {
    const done = () => { button.textContent = "Copied"; setTimeout(() => { button.textContent = "Copy JSON"; }, 1500); };
    try {
      navigator.clipboard.writeText(text).then(done, () => {});
    } catch (err) {
      // no clipboard access: the JSON stays selectable in the block
    }
  }

  function renderMessage(message, row) {
    const kind = message.kind;
    const customer = (row.envelope && row.envelope.customer_id) || "—";
    const head = el("h3", {}, `${agentLabel(message.source)} → ${agentLabel(message.target)}`);
    const meta = el("p", { class: "viewer-meta" },
      [kind, message.device_id, customer, hms(message.time_ms),
       `${message.topic} @ ${message.partition}:${message.offset}`].join(" · "));
    const list = el("dl", { class: "viewer-fields" });
    for (const name of READABLE_FIELDS[kind] || []) {
      if (isEmpty(row[name])) continue;
      list.append(el("dt", {}, name), el("dd", {}, fieldText(row[name])));
    }
    const json = JSON.stringify(row, null, 2);
    const copy = el("button", { type: "button", class: "viewer-copy" }, "Copy JSON");
    copy.addEventListener("click", () => copyJson(json, copy));
    viewerBody.replaceChildren(head, meta, list, copy, el("pre", { class: "viewer-json" }, json));
  }

  async function openMessage(messageId) {
    const request = ++viewerRequest;
    viewerNote("loading…");
    if (!viewer.open) viewer.showModal();
    try {
      const response = await fetch(`/messages/${encodeURIComponent(messageId)}`);
      const body = await response.json();
      if (request !== viewerRequest) return;
      if (response.status === 404) {
        viewerNote(`no longer held — the UI keeps the most recent ${body.limit} messages`);
      } else if (response.ok) {
        renderMessage(body.message, body.row);
      } else {
        viewerNote("could not load the message");
      }
    } catch (err) {
      if (request === viewerRequest) viewerNote("could not load the message");
    }
  }

  function openFrom(e, selector) {
    const target = e.target.closest(selector);
    if (target && target.dataset.messageId) openMessage(target.dataset.messageId);
  }

  activityCards.addEventListener("click", (e) => openFrom(e, ".bubble"));
  feedBody.addEventListener("click", (e) => openFrom(e, "tr.feed-row"));
  $(".viewer-close", viewer).addEventListener("click", () => viewer.close());
  viewer.addEventListener("click", (e) => { if (e.target === viewer) viewer.close(); });

  // ---- explainer ----
  const explainer = $("#explainer");
  $("#explain-open").addEventListener("click", () => explainer.showModal());
  $(".viewer-close", explainer).addEventListener("click", () => explainer.close());
  explainer.addEventListener("click", (e) => { if (e.target === explainer) explainer.close(); });

  // ---- stream ----
  function onSnapshot(snap) {
    const filter = state.filter;
    state = freshState();
    resetGraph();
    for (const row of snap.devices) upsertDevice(row, false);
    for (const m of [...snap.feed].reverse()) addMessage(m);
    for (const r of snap.pairs.ranking) addEdge(r.from_agent, r.to_agent);
    state.pairs = snap.pairs;
    state.summary = snap.summary;
    state.stale = { ...state.stale, ...snap.stale };
    renderActivity(snap.activity || {});
    renderDevices(new Set());
    renderSummary();
    renderPairs();
    renderEdgeWeights();
    renderStale();
    renderEmpty();
    setFilter(filter);
  }

  function onBatch(items) {
    const flash = new Set();
    const messages = [];
    const dirty = new Set();
    for (const { type, data } of items) {
      if (type === "device") {
        if (upsertDevice(data, true)) flash.add(data.device_id);
        dirty.add("devices");
      } else if (type === "message") {
        if (addMessage(data)) messages.push(data);
      } else if (type === "pairs") {
        state.pairs = data;
        for (const r of data.ranking) addEdge(r.from_agent, r.to_agent);
        dirty.add("pairs");
      } else if (type === "summary") {
        state.summary = data;
        dirty.add("summary");
      } else if (type === "activity") {
        offerBubble(data);
      } else if (type === "stale") {
        state.stale[data.source] = data.since_ms;
        dirty.add("stale");
      }
    }
    if (dirty.has("devices")) renderDevices(flash);
    if (dirty.has("summary")) renderSummary();
    if (dirty.has("pairs")) { renderPairs(); renderEdgeWeights(); }
    if (dirty.has("stale")) renderStale();
    if (messages.length) {
      prependFeed(messages);
      renderEmpty();
      scheduleVisuals(messages);
    }
  }

  const conn = $("#conn");
  function setConn(text, cls) {
    conn.textContent = text;
    conn.className = `conn ${cls}`;
  }

  // A payload that is not valid JSON cannot be applied, and the page would drift from the server if later
  // batches were applied on top of it. The stream is closed and reopened, and the server answers with a snapshot.
  let source = null;
  let resyncTimer = null;

  function resync() {
    if (source) source.close();
    source = null;
    setConn("resyncing…", "down");
    if (resyncTimer === null) resyncTimer = setTimeout(() => { resyncTimer = null; connect(); }, RESYNC_MS);
  }

  function handler(apply) {
    return (e) => {
      let data;
      try {
        data = JSON.parse(e.data);
      } catch (err) {
        console.error(`dropping an unparseable ${e.type} event`, err);
        resync();
        return;
      }
      apply(data);
    };
  }

  function connect() {
    source = new EventSource("/events");
    source.addEventListener("open", () => setConn("live", "live"));
    source.addEventListener("error", () => setConn("reconnecting…", "down"));
    source.addEventListener("snapshot", handler(onSnapshot));
    source.addEventListener("batch", handler(onBatch));
  }

  connect();

  resetGraph();
  renderEmpty();
  renderActivity({});
  requestAnimationFrame(frame);
})();
