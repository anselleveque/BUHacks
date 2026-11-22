import { pollDemoState } from "./poll.js";

// Director panel for the demo server. Talks to /demo/api/* without cookies; never touches the app's UI.
const api = "/demo/api";
const pinnedKey = "midnight-munch-pinned-trip";
const beatsKey = "midnight-munch-director-beats";
const byId = (id) => document.getElementById(id);
let state = null;
let catalog = null;
let receivedAt = 0;
let needsCatalog = true;
const fields = byId("controlFields");

function element(tag, text, parent, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  parent?.append(node);
  return node;
}

function showResult(message, error = false) {
  const node = byId("result");
  node.textContent = message;
  node.classList.toggle("error", error);
}

async function post(path, body = {}) {
  if (fields.disabled) return null;
  fields.disabled = true;
  try {
    const response = await fetch(`${api}/${path}`, {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    apply(data);
    describe(data.last_action);
    return data;
  } catch (error) {
    showResult(error.message || "The demo server did not answer.", true);
    return null;
  } finally {
    fields.disabled = false;
    poll();
  }
}

function describe(action) {
  if (!action) return;
  const result = action.result;
  if (action.id === "missed-train") showResult(`Clock moved just past the ${formatTime(action.board_iso)} boarding.`);
  else if (result?.kind === "price-report") {
    showResult(result.accepted ? `Price $${(result.accepted_cents / 100).toFixed(2)} accepted (${result.votes} reports).`
      : `Second diner reported $${(result.cents / 100).toFixed(2)}: ${result.votes} of ${result.needed} reports.`);
  } else if (result?.kind === "item-removal") {
    showResult(result.removed ? "Item removed by two reports." : `Removal: ${result.remove_votes} of ${result.needed} reports.`);
  } else if (result?.kind === "community-dish") {
    showResult(result.accepted ? `${result.name} is now a community dish.` : `${result.name} logged by a second diner; one more report needed.`);
  } else if (action.kind === "scenario") showResult(`Scenario ${action.id} loaded.`);
  else if (action.kind === "persona" || action.kind === "restore") showResult(`Persona: ${state?.persona.name ?? action.id}.`);
  else if (action.kind === "alert") showResult(`Alert ${action.on ? "on" : "off"}.`);
  else showResult("Done.");
}

const timeFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit", second: "2-digit"
});
const shortTime = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
const formatTime = (iso) => Number.isFinite(Date.parse(iso)) ? shortTime.format(Date.parse(iso)) : "";

function demoNow() {
  if (!state) return NaN;
  const { epoch_ms: epoch, running, speed } = state.clock;
  return epoch + (running ? (performance.now() - receivedAt) * speed : 0);
}

function apply(next) {
  if (!next || next.type !== "state") return;
  const isNew = !state || next.seq > state.seq || next.app_epoch !== state.app_epoch;
  if (isNew) {
    state = next;
    receivedAt = performance.now();
    if (next.reason === "reset") needsCatalog = true;
  }
  if (next.catalog) {
    if (!catalog) fields.disabled = false;
    catalog = next.catalog;
    needsCatalog = false;
    build();
  }
  if (isNew || next.catalog) render();
}

function render() {
  if (!state) return;
  const { clock } = state;
  byId("clockMode").textContent = clock.running ? (clock.speed > 1 ? `Running ${clock.speed}x` : "Running") : "Frozen";
  byId("clockMode").classList.toggle("running", clock.running);
  byId("playPause").textContent = clock.running ? "Freeze" : "Play";
  byId("scenarioName").textContent = `${state.scenario.id}. ${state.scenario.name}`;
  byId("personaName").textContent = state.persona.name;
  const labels = new Map((catalog?.alerts ?? []).map((alert) => [alert.id, alert.label]));
  byId("alertNames").textContent = state.alerts.map((id) => labels.get(id) ?? id).join(", ") || "None";
  byId("hint").textContent = state.scenario.hint;
  const dish = byId("dishScript");
  dish.setAttribute("aria-pressed", String(state.dish_script));
  dish.textContent = state.dish_script ? "Dish script on" : "Dish script off";
  for (const button of document.querySelectorAll("[data-scenario]")) {
    button.classList.toggle("active", Number(button.dataset.scenario) === state.scenario.id);
  }
  for (const button of document.querySelectorAll("[data-persona]")) {
    button.setAttribute("aria-pressed", String(button.dataset.persona === state.persona.id));
  }
  for (const button of document.querySelectorAll("[data-alert]")) {
    button.setAttribute("aria-pressed", String(state.alerts.includes(button.dataset.alert)));
  }
  for (const button of document.querySelectorAll("[data-speed]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.speed) === clock.speed));
  }
  const persona = catalog?.personas.find(({ id }) => id === state.persona.id);
  const code = byId("recoveryCode");
  code.hidden = !persona?.recovery_code;
  code.textContent = persona?.recovery_code ? `${persona.name}'s recovery code: ${persona.recovery_code}` : "";
  renderBeats();
  tick();
}

function tick() {
  const now = demoNow();
  if (Number.isFinite(now)) byId("clockLabel").textContent = timeFormat.format(now);
}

function build() {
  const scenarios = byId("scenarios");
  scenarios.replaceChildren();
  for (const scenario of catalog.scenarios) {
    const button = element("button", undefined, scenarios);
    button.type = "button";
    button.dataset.scenario = scenario.id;
    element("span", String(scenario.id), button, "number");
    button.append(` ${scenario.name}`);
    element("small", new Date(scenario.at).toLocaleString("en-US", {
      timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit"
    }), button);
    button.addEventListener("click", () => post(`scenario/${scenario.id}`));
  }
  const personas = byId("personas");
  personas.replaceChildren();
  for (const persona of catalog.personas) {
    const button = element("button", persona.name, personas);
    button.type = "button";
    button.dataset.persona = persona.id;
    button.title = persona.summary;
    element("small", persona.summary, button);
    button.addEventListener("click", () => post(`persona/${persona.id}`));
  }
  const alerts = byId("alerts");
  alerts.replaceChildren();
  for (const alert of catalog.alerts) {
    const button = element("button", alert.label, alerts);
    button.type = "button";
    button.dataset.alert = alert.id;
    button.title = alert.header;
    button.addEventListener("click", () => post(`alert/${alert.id}`, { on: !state.alerts.includes(alert.id) }));
  }
  const speeds = byId("speeds");
  speeds.replaceChildren();
  for (const speed of catalog.speeds) {
    const button = element("button", `${speed}x`, speeds);
    button.type = "button";
    button.dataset.speed = speed;
    button.addEventListener("click", async () => {
      // Picking a speed means "run at this speed": resume a frozen clock first.
      if (state && !state.clock.running) await post("clock", { action: "play" });
      post("clock", { action: "speed", speed });
    });
  }
}

// Beats -------------------------------------------------------------------------------------------------

function checkedBeats() {
  try { return new Set(JSON.parse(localStorage.getItem(beatsKey)) ?? []); } catch { return new Set(); }
}

function saveBeats(checked) {
  try { localStorage.setItem(beatsKey, JSON.stringify([...checked])); } catch { /* Checks stay for this view. */ }
}

function runAction(action) {
  if (!action) return Promise.resolve();
  const [kind, value] = action.split(":");
  if (kind === "scenario") return post(`scenario/${value}`);
  if (kind === "persona") return post(`persona/${value}`);
  if (kind === "speed") return post("clock", { action: "speed", speed: Number(value) });
  if (kind === "trigger") return value === "missed-train" ? missedTrain() : post(`trigger/${value}`);
  return Promise.resolve();
}

function renderBeats() {
  if (!catalog) return;
  const list = byId("beats");
  const checked = checkedBeats();
  const next = catalog.beats.find((beat) => !checked.has(beat.id));
  list.replaceChildren();
  for (const beat of catalog.beats) {
    const item = element("li", undefined, list, checked.has(beat.id) ? "done" : beat === next ? "next" : "");
    if (beat === next) item.setAttribute("aria-current", "step");
    element("span", beat.at, item, "time");
    const label = element("label", undefined, item);
    const box = element("input", undefined, label);
    box.type = "checkbox";
    box.checked = checked.has(beat.id);
    box.addEventListener("change", () => {
      const current = checkedBeats();
      box.checked ? current.add(beat.id) : current.delete(beat.id);
      saveBeats(current);
      renderBeats();
    });
    label.append(beat.label);
    if (beat.action) {
      const run = element("button", beat === next ? "Run (N)" : "Run", item);
      run.type = "button";
      run.addEventListener("click", () => runAction(beat.action));
    }
  }
}

function runNextBeat() {
  const checked = checkedBeats();
  const next = catalog?.beats.find((beat) => !checked.has(beat.id));
  if (!next) return;
  checked.add(next.id);
  saveBeats(checked);
  runAction(next.action).then(renderBeats);
}

// Moments -----------------------------------------------------------------------------------------------

function pinnedBoarding() {
  // The panel shares the app's origin, so it can read the pinned trip the app saved.
  try {
    const trip = JSON.parse(localStorage.getItem(pinnedKey));
    const ride = trip?.outbound?.legs?.find((leg) => leg.mode === "ride");
    return Number.isFinite(Date.parse(ride?.depart_iso)) ? ride.depart_iso : undefined;
  } catch {
    return undefined;
  }
}

function missedTrain() {
  const board = pinnedBoarding();
  return post("trigger/missed-train", board ? { board_iso: board } : {});
}

function setConnection(status, error) {
  const node = byId("connection");
  const online = status === "Live";
  if (status) node.textContent = status;
  node.classList.toggle("online", online);
  node.classList.toggle("offline", !online);
  byId("connectionError").textContent = error;

}

// Wiring ------------------------------------------------------------------------------------------------

function toggleControls() {
  const main = byId("controls");
  main.hidden = !main.hidden;
  const button = byId("toggleControls");
  button.setAttribute("aria-expanded", String(!main.hidden));
  button.textContent = main.hidden ? "Show controls" : "Hide controls";
}

byId("playPause").addEventListener("click", () => post("clock", { action: state?.clock.running ? "freeze" : "play" }));
for (const button of document.querySelectorAll("[data-jump]")) {
  button.addEventListener("click", () => post("clock", { action: "jump", minutes: Number(button.dataset.jump) }));
}
for (const button of document.querySelectorAll("[data-trigger]")) {
  button.addEventListener("click", () => post(`trigger/${button.dataset.trigger}`));
}
byId("setTime").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = byId("timeInput").value;
  if (value) post("clock", { action: "set", time: value });
});
byId("missedTrain").addEventListener("click", missedTrain);
byId("dishScript").addEventListener("click", () => post("trigger/dish-script", { on: !state?.dish_script }));
byId("resetScenario").addEventListener("click", () => post("reset", { scope: "scenario" }));
byId("resetEverything").addEventListener("click", () => {
  if (confirm("Rebuild the demo database and go back to scenario 1?")) post("reset", { scope: "everything" });
});
byId("toggleControls").addEventListener("click", toggleControls);
byId("clearBeats").addEventListener("click", () => { saveBeats(new Set()); renderBeats(); });

document.addEventListener("keydown", async (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest?.("input, textarea, select")) return;
  const key = event.key;
  if (fields.disabled && !/^[hH]$/.test(key)) return;
  if (/^[1-9]$/.test(key)) post(`scenario/${key}`);
  else if (key === " ") {
    event.preventDefault();
    post("clock", { action: state?.clock.running ? "freeze" : "play" });
  }
  else if (key === "j" || key === "J") post("clock", { action: "jump", minutes: 1 });
  else if (key === "k" || key === "K") post("clock", { action: "jump", minutes: 5 });
  else if (key === "l" || key === "L") post("clock", { action: "jump", minutes: 15 });
  else if (key === ";") post("clock", { action: "jump", minutes: 60 });
  else if (key === "x" || key === "X") {
    const speeds = catalog?.speeds ?? [1, 5, 20, 60];
    const speed = speeds[(speeds.indexOf(state?.clock.speed) + 1) % speeds.length];
    if (state && !state.clock.running) await post("clock", { action: "play" });
    post("clock", { action: "speed", speed });
  } else if (key === "m" || key === "M") missedTrain();
  else if (key === "p" || key === "P") post("trigger/price-report");
  else if (key === "r" || key === "R") post("reset", { scope: "scenario" });
  else if (key === "n" || key === "N") runNextBeat();
  else if (key === "h" || key === "H") toggleControls();
  else return;
});

setInterval(tick, 250);
const poll = pollDemoState({
  path: () => needsCatalog ? "state" : "state?lite=1",
  accept: apply,
  connection: setConnection
});
