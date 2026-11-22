"use strict";

// The app-side demo client. Loaded only when GET /api/options returns demo.active (the demo server);
// production never loads it, and the production server refuses to serve it. It subscribes to
// /demo/api/events and exposes the demo clock as window.midnightDemo. The app reads it through the
// hook points listed in docs/DEMO.md ("App hook points"); nothing here changes the app's UI except the
// optional ?director=1 mini controller.
(() => {
  if (window.midnightDemo) return;
  const api = "/demo/api";
  const epochKey = "midnight-demo-epoch";
  const profileKey = "midnight-munch-profile";
  // Per-viewer state that belongs to the previous scenario or persona.
  const scenarioKeys = ["midnight-munch-pinned-trip", "midnight-munch-pinned-history", "midnight-munch-recent-stations",
    "midnight-munch-walk-ok", "midnight-munch-fare-trips"];
  const JUMP_MS = 90000;
  const listeners = new Map();
  let state = null;
  let receivedAt = 0;
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });

  function now() {
    if (!state) return Date.now();
    const { epoch_ms: epoch, running, speed } = state.clock;
    return epoch + (running ? (performance.now() - receivedAt) * speed : 0);
  }

  function emit(type, detail) {
    for (const callback of listeners.get(type) ?? []) {
      try { callback(detail); } catch (error) { console.error(error); }
    }
    window.dispatchEvent(new CustomEvent(`midnight-demo:${type}`, { detail }));
  }

  function on(type, callback) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(callback);
    return () => listeners.get(type).delete(callback);
  }

  // The app applies these saved defaults on load (web/community.js applyDefault), so a scenario's
  // start station, budget, stay, walk limit and late-night mode take effect after one reload.
  function localProfile(next) {
    const app = next.scenario.app;
    const saved = next.persona.profile ?? {};
    return {
      name: saved.display_name ?? "", avatar: saved.avatar ?? "🐶", color: saved.color ?? "blue", bio: saved.bio ?? "",
      defaults: {
        station: app.start_station_id, budget: String(app.budget), stay: String(app.stay_min),
        walk: String(app.walk_ok_min), late: app.late_mode, rideHome: app.count_ride_home ? "yes" : "no"
      }
    };
  }

  function applyScenario(next) {
    // Returns true when the page is reloading into the new scenario or persona.
    let stored;
    try { stored = sessionStorage.getItem(epochKey); } catch { return false; }
    const epoch = `${next.app_epoch}`;
    if (stored === epoch) return false;
    try {
      for (const key of scenarioKeys) localStorage.removeItem(key);
      localStorage.setItem(profileKey, JSON.stringify(localProfile(next)));
      sessionStorage.setItem(epochKey, epoch);
    } catch {
      return false;
    }
    emit("reload", next);
    location.reload();
    return true;
  }

  function accept(next) {
    if (!next || next.type !== "state") return;
    const before = state ? now() : null;
    state = next;
    receivedAt = performance.now();
    window.midnightDemo.state = next;
    resolveReady(next);
    if (applyScenario(next)) return;
    emit("state", next);
    if (before !== null && Math.abs(next.clock.epoch_ms - before) > JUMP_MS) {
      emit("jump", { from: before, to: next.clock.epoch_ms, reason: next.reason });
    }
    if (next.replan && next.reason !== "tick") emit("replan", next);
    renderMini();
  }

  async function listen() {
    // fetch() instead of EventSource: the demo API refuses requests that carry cookies.
    for (;;) {
      try {
        const response = await fetch(`${api}/events`, { credentials: "omit", cache: "no-store" });
        if (!response.ok || !response.body) throw new Error("No event stream.");
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let end;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const data = block.split("\n").filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trimStart()).join("\n");
            if (data) {
              try { accept(JSON.parse(data)); } catch { /* Skip a malformed event. */ }
            }
          }
        }
      } catch {
        // Reconnect below; the app keeps using the last known clock meanwhile.
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }

  // Optional mini controller for single-screen use: add ?director=1 to the app's address. --------------

  let mini = null;

  async function control(path, body = {}) {
    try {
      const response = await fetch(`${api}/${path}`, {
        method: "POST", credentials: "omit", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok) accept(data);
      else if (mini) mini.status.textContent = data.error ?? "Not allowed.";
    } catch {
      if (mini) mini.status.textContent = "Demo server unreachable.";
    }
  }

  function button(parent, label, onClick) {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = label;
    node.addEventListener("click", onClick);
    parent.append(node);
    return node;
  }

  function buildMini() {
    const host = document.createElement("div");
    host.setAttribute("role", "region");
    host.setAttribute("aria-label", "Demo controls");
    const shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = `
      :host { all: initial; position: fixed; right: 12px; bottom: 96px; z-index: 2147483000;
        --bg: #131b1a; --surface: #1d2623; --border: #232c2b; --lime: #dfe9b1; --text: #f1f4ee; --muted: #aab3ac; }
      .box { font: 13px/1.3 Inter, system-ui, sans-serif; color: var(--text); background: var(--surface);
        border: 1px solid var(--border); border-radius: 12px; padding: 8px; display: grid; gap: 6px; width: 220px;
        box-shadow: 0 8px 24px rgb(0 0 0 / .4); }
      .time { font-size: 18px; font-weight: 600; color: #8fd3a8; font-variant-numeric: tabular-nums; }
      .row { display: flex; gap: 4px; flex-wrap: wrap; }
      button, select { font: inherit; color: var(--text); background: var(--bg); border: 1px solid #2f3837;
        border-radius: 8px; min-height: 32px; padding: 2px 8px; cursor: pointer; }
      button:focus-visible, select:focus-visible { outline: 2px solid var(--lime); outline-offset: 2px; }
      .status { color: var(--muted); min-height: 1em; }
      [hidden] { display: none; }`;
    const box = document.createElement("div");
    box.className = "box";
    const time = document.createElement("div");
    time.className = "time";
    const row = document.createElement("div");
    row.className = "row";
    const play = button(row, "Freeze", () => control("clock", { action: state?.clock.running ? "freeze" : "play" }));
    button(row, "+5", () => control("clock", { action: "jump", minutes: 5 }));
    button(row, "+15", () => control("clock", { action: "jump", minutes: 15 }));
    const hide = button(row, "Hide", () => {
      const hidden = !row.nextElementSibling.hidden;
      for (const node of [select, status]) node.hidden = hidden;
      hide.textContent = hidden ? "Show" : "Hide";
    });
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Scenario");
    const names = ["Late-night hero flow", "Missed train", "Last train home warning", "No trains at 4 AM", "Shuttle alert",
      "Walk or ride at night", "Closed places", "Community moment", "Profile and stamps"];
    names.forEach((name, index) => {
      const option = document.createElement("option");
      option.value = String(index + 1);
      option.textContent = `${index + 1}. ${name}`;
      select.append(option);
    });
    select.addEventListener("change", () => control(`scenario/${select.value}`));
    const status = document.createElement("div");
    status.className = "status";
    status.setAttribute("aria-live", "polite");
    box.append(time, row, select, status);
    shadow.append(style, box);
    document.body.append(host);
    return { time, play, select, status };
  }

  const clockText = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit", second: "2-digit"
  });

  function renderMini() {
    if (!mini || !state) return;
    mini.time.textContent = clockText.format(now());
    mini.play.textContent = state.clock.running ? "Freeze" : "Play";
    mini.select.value = String(state.scenario.id);
  }

  window.midnightDemo = { active: true, now, on, ready, state: null };
  if (new URLSearchParams(location.search).get("director") === "1") {
    const start = () => { mini = buildMini(); setInterval(renderMini, 500); };
    document.body ? start() : document.addEventListener("DOMContentLoaded", start, { once: true });
  }
  listen();
})();
