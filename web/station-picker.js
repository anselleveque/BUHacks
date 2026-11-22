"use strict";

const stationInput = byId("stationSearch");
const stationPopup = byId("stationPopup");
const stationList = byId("stationList");
const clearStation = byId("clearStation");
const recentStationsKey = "midnight-munch-recent-stations";
const cachedStationsKey = "midnight-munch-station-catalog";
const recentStationRetention = 6; // Leave room to omit the separately listed home station.
const stationNicknames = new Map([
  ["bu", "boston university"], ["harvard sq", ["place-harsq"]],
  ["mgh", ["place-chmnl"]], ["mit", ["place-knncl"]],
  ["southie", ["place-brdwy", "place-andrw"]]
]);
const stationCollator = new Intl.Collator("en", { sensitivity: "base", numeric: true });
const stationLabel = (stop) => `${stop.name} (${stop.lines.join(", ")})`;
const stationWords = (text) => text.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase()
  .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
let stationRows = [];
let activeStation = -1;
let stationFocusHeight = 0;
let stationFrame = 0;

function readRecentStations() {
  try {
    const ids = JSON.parse(localStorage.getItem(recentStationsKey));
    return Array.isArray(ids) ? [...new Set(ids.filter((id) => stations.has(id)))].slice(0, recentStationRetention) : [];
  } catch {
    return [];
  }
}

function rememberStation(id) {
  try {
    localStorage.setItem(recentStationsKey, JSON.stringify([id, ...readRecentStations().filter((previous) => previous !== id)].slice(0, recentStationRetention)));
  } catch {
    // The station remains usable when browser storage is unavailable.
  }
}

// One insertion, deletion, replacement, or swap of adjacent letters.
function oneStationEdit(left, right) {
  if (Math.abs(left.length - right.length) > 1) return false;
  if (left.length < right.length) return oneStationEdit(right, left);
  const first = [...left].findIndex((letter, index) => letter !== right[index]);
  if (first < 0) return left.length === right.length;
  if (left.length > right.length) return left.slice(first + 1) === right.slice(first);
  return left.slice(first + 1) === right.slice(first + 1) ||
    left[first] === right[first + 1] && left[first + 1] === right[first] && left.slice(first + 2) === right.slice(first + 2);
}

function stationQuery(value) {
  const raw = stationWords(value);
  const compact = raw.replaceAll(" ", "");
  const nickname = stationNicknames.get(raw) ?? stationNicknames.get(compact);
  const expanded = raw.replace(/^b ?u(?= |$)/, stationNicknames.get("bu"));
  return { raw, expanded, words: expanded.split(" "), nicknames: Array.isArray(nickname) ? nickname : [] };
}

function matchStation(stop, query) {
  const name = stationWords(stop.name);
  const label = stationWords(stationLabel(stop));
  const lines = stop.lines.map(stationWords);
  const lineQuery = query.raw.replace(/^(the )?/, "").replace(/ line$/, "");
  const compact = (text) => text.replaceAll(" ", "");
  if (query.raw === name || query.raw === label || compact(query.raw) === compact(name)) return { rank: 0, line: null };
  const line = lines.find((color) => color === lineQuery);
  if (line) return { rank: 0, line };
  if (query.nicknames.includes(stop.id)) return { rank: 0, line: null, nickname: true };
  if (name.startsWith(query.expanded) || label.startsWith(query.expanded) || compact(name).startsWith(compact(query.expanded))) return { rank: 1, line: null };
  const linePrefix = lines.find((color) => lineQuery && color.startsWith(lineQuery));
  const words = name.split(" ");
  const matchWords = (predicate) => query.words.every((word) => words.some((nameWord) => predicate(word, nameWord)));
  if (matchWords((word, nameWord) => nameWord.startsWith(word))) return { rank: 2, line: null };
  if (linePrefix) return { rank: 2, line: linePrefix };
  if (matchWords((word, nameWord) => nameWord.startsWith(word) || word.length >= 5 && oneStationEdit(word, nameWord))) {
    return { rank: 3, line: null };
  }
  return null;
}

function distanceBetweenStations(stop, point) {
  return map.distance(coordinates(stop), coordinates(point));
}

function compareStationNames(left, right) {
  return stationCollator.compare(left.name, right.name) || stationCollator.compare(left.id, right.id);
}

function compareStationDistance(left, right) {
  return (locationPoint ? distanceBetweenStations(left, locationPoint) - distanceBetweenStations(right, locationPoint) : 0) ||
    compareStationNames(left, right);
}

function stationGroups(value) {
  const query = stationQuery(value);
  if (query.raw) {
    const matches = [...stations.values()].map((stop) => {
      const match = matchStation(stop, query);
      return match ? { stop, query, ...match } : null;
    }).filter(Boolean).sort((left, right) => left.rank - right.rank || compareStationDistance(left.stop, right.stop));
    return [{ name: "", matches: matches.slice(0, 8) }];
  }
  const used = new Set();
  const group = (name, candidates, limit) => {
    const stops = candidates.filter((stop) => stop && !used.has(stop.id)).slice(0, limit);
    stops.forEach((stop) => used.add(stop.id));
    return { name, matches: stops.map((stop) => ({ stop })) };
  };
  return [
    group("Home station", [stations.get(profile.defaults.station)], 1),
    group("Recent starts", readRecentStations().map((id) => stations.get(id)), 5),
    group("Nearest stations", locationPoint ? [...stations.values()].sort(compareStationDistance) : [], 5)
  ];
}

function appendStationName(parent, { stop, query, nickname, rank }) {
  if (nickname || rank === 3) {
    element("strong", stop.name, parent);
    return;
  }
  // Split the display name at word boundaries without losing punctuation or accents.
  for (const chunk of stop.name.split(/([\p{L}\p{N}\p{M}]+)/u)) {
    const word = stationWords(chunk);
    const prefix = query?.words.filter((part) => word && word.startsWith(part)).sort((a, b) => b.length - a.length)[0];
    if (!prefix) {
      parent.append(document.createTextNode(chunk));
      continue;
    }
    let end = 0;
    while (end < chunk.length && stationWords(chunk.slice(0, end)).length < prefix.length) end++;
    while (/\p{M}/u.test(chunk[end] ?? "")) end++;
    element("strong", chunk.slice(0, end), parent);
    parent.append(document.createTextNode(chunk.slice(end)));
  }
}

function renderStationPopup() {
  stationList.replaceChildren();
  stationRows = [];
  activeStation = -1;
  stationInput.removeAttribute("aria-activedescendant");
  for (const { name, matches } of stationGroups(stationInput.value)) {
    if (!matches.length) continue;
    const group = name ? element("div", "", stationList) : stationList;
    if (name) {
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", name);
      const heading = element("div", name, group);
      heading.className = "station-group";
      heading.setAttribute("aria-hidden", "true");
    }
    for (const match of matches) {
      const { stop, line } = match;
      const node = element("div", "", group);
      node.id = `station-option-${stationRows.length}`;
      node.className = "station-option";
      node.setAttribute("role", "option");
      node.setAttribute("aria-selected", "false");
      const name = element("span", "", node);
      appendStationName(name, match);
      const chips = element("span", "", node);
      chips.className = "station-lines";
      for (const color of stop.lines) {
        const chip = element(line === stationWords(color) ? "strong" : "span", color, chips);
        chip.className = "station-line";
        chip.dataset.line = stationWords(color);
      }
      node.addEventListener("mousedown", (event) => event.preventDefault());
      node.addEventListener("click", (event) => {
        chooseStation(stop);
        if (event.pointerType === "touch") stationInput.blur();
      });
      stationRows.push({ node, stop });
    }
  }
  const count = stationRows.length;
  const raw = stationInput.value.trim();
  const message = raw ? `No station matches '${raw}'. Try a line or a neighborhood.` : "Type a station, line or neighborhood.";
  byId("stationNoMatch").hidden = Boolean(count);
  setText("stationNoMatch", count ? "" : message);
  stationPopup.hidden = false;
  stationInput.setAttribute("aria-expanded", "true");
  setText("stationAnnouncement", count ? `${count} ${count === 1 ? "station" : "stations"}, use up and down` : message);
  scheduleStationViewport();
}

function syncStationInput() {
  const stop = stations.get(station.value);
  stationInput.value = stop ? stationLabel(stop) : "";
  setText("stationDisplay", stop?.id === "place-bucen" ? "BU Central · Green B" : stationInput.value);
  clearStation.hidden = !stationInput.value;
}

function closeStationPopup(restore = false) {
  stationPopup.hidden = true;
  stationInput.setAttribute("aria-expanded", "false");
  stationInput.removeAttribute("aria-activedescendant");
  stationRows[activeStation]?.node.setAttribute("aria-selected", "false");
  activeStation = -1;
  setText("stationAnnouncement", "");
  if (restore) syncStationInput();
  byId("stationKeyboardLayout").classList.remove("station-keyboard");
}

function chooseStation(stop, remember = true) {
  closeStationPopup();
  station.value = stop.id;
  if (remember) rememberStation(stop.id);
  station.dispatchEvent(new Event("change", { bubbles: true }));
}

function highlightStation(index) {
  if (!stationRows.length) return;
  stationRows[activeStation]?.node.setAttribute("aria-selected", "false");
  activeStation = (index + stationRows.length) % stationRows.length;
  const { node } = stationRows[activeStation];
  node.setAttribute("aria-selected", "true");
  stationInput.setAttribute("aria-activedescendant", node.id);
  const rect = node.getBoundingClientRect(), list = stationList.getBoundingClientRect();
  if (rect.top < list.top) stationList.scrollTop += rect.top - list.top;
  if (rect.bottom > list.bottom) stationList.scrollTop += rect.bottom - list.bottom;
}

stationInput.addEventListener("focus", () => {
  stationFocusHeight = window.innerHeight;
  stationInput.select();
  renderStationPopup();
});
stationInput.addEventListener("input", (event) => {
  clearStation.hidden = !stationInput.value && !station.value;
  if (!event.isComposing) renderStationPopup();
});
stationInput.addEventListener("compositionend", renderStationPopup);
stationInput.addEventListener("blur", () => closeStationPopup(true));
stationInput.addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  const key = event.key;
  if (key === "ArrowDown" || key === "ArrowUp") {
    event.preventDefault();
    if (stationPopup.hidden) renderStationPopup();
    highlightStation(activeStation < 0 ? (key === "ArrowDown" ? 0 : stationRows.length - 1) :
      activeStation + (key === "ArrowDown" ? 1 : -1));
  } else if (!stationPopup.hidden && (key === "Home" || key === "End") && activeStation >= 0) {
    event.preventDefault();
    highlightStation(key === "Home" ? 0 : stationRows.length - 1);
  } else if (key === "Enter") {
    event.preventDefault(); // Typing in the station field never submits the plan.
    if (!stationPopup.hidden) {
      const match = stationRows[activeStation] ?? (stationRows.length === 1 ? stationRows[0] : null);
      if (match) chooseStation(match.stop);
    }
  } else if (key === "Escape" && !stationPopup.hidden) {
    event.preventDefault();
    event.stopPropagation();
    closeStationPopup(true);
  } else if (key === "Tab") {
    closeStationPopup(true);
  }
});
clearStation.addEventListener("mousedown", (event) => event.preventDefault());
clearStation.addEventListener("click", () => {
  if (station.value) {
    station.value = "";
    station.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    syncStationInput();
  }
  stationInput.focus();
  renderStationPopup();
});
document.addEventListener("start-location-changed", () => {
  syncStationInput();
  closeStationPopup();
});

function scheduleStationViewport() {
  cancelAnimationFrame(stationFrame);
  stationFrame = requestAnimationFrame(fitStationPopup);
}

function fitStationPopup() {
  if (stationPopup.hidden || document.activeElement !== stationInput) return;
  const viewport = window.visualViewport;
  const height = viewport?.height ?? window.innerHeight;
  const keyboard = window.innerWidth < 900 && Math.max(stationFocusHeight, window.innerHeight) - height > 100;
  const layout = byId("stationKeyboardLayout");
  layout.classList.toggle("station-keyboard", keyboard);
  if (keyboard) layout.style.setProperty("--station-viewport", `${height}px`);
  const bottom = (viewport?.offsetTop ?? 0) + height;
  stationInput.scrollIntoView({ block: "nearest", inline: "nearest" });
  const available = Math.min(bottom, byId("planPrimary").getBoundingClientRect().top) - stationPopup.getBoundingClientRect().top;
  stationList.style.maxHeight = `${Math.max(54, Math.min(320, available - 8))}px`;
}

window.addEventListener("resize", scheduleStationViewport);
window.visualViewport?.addEventListener("resize", scheduleStationViewport);
window.visualViewport?.addEventListener("scroll", scheduleStationViewport);

async function loadStations() {
  byId("retryStations").hidden = true;
  const addStations = (data) => {
    if (!Array.isArray(data) || !data.length) throw new Error("No stations available.");
    for (const stop of data.filter((entry) => entry && typeof entry.id === "string" && typeof entry.name === "string" &&
      hasCoordinates(entry) && Math.abs(entry.lat) <= 90 && Math.abs(entry.lng) <= 180 && Array.isArray(entry.lines) && entry.lines.every((line) => typeof line === "string")).sort(compareStationNames)) {
      if (stations.has(stop.id)) continue;
      stations.set(stop.id, stop);
      station.add(new Option(stationLabel(stop), stop.id));
    }
    if (!stations.size) throw new Error("No stations available.");
  };
  const loadCatalog = async (params) => {
    addStations(await get("data/stations.json", params));
    try { localStorage.setItem(cachedStationsKey, JSON.stringify([...stations.values()])); } catch {}
  };
  try {
    await loadCatalog();
    station.hidden = false;
    station.classList.add("sr-only");
    station.setAttribute("aria-hidden", "true");
    station.tabIndex = -1;
    byId("stationInputGroup").hidden = false;
    byId("stationLabel").htmlFor = "stationSearch";
    stationInput.disabled = false;
    setText("locationHelp", "Device location requires localhost or HTTPS. You can also choose a T station.");
    setText("stationFallbackStatus", "");
  } catch {
    closeStationPopup(true);
    byId("stationInputGroup").hidden = true;
    station.hidden = false;
    station.classList.remove("sr-only");
    station.removeAttribute("aria-hidden");
    station.removeAttribute("tabindex");
    byId("stationLabel").htmlFor = "station";
    setText("stationFallbackStatus", "Station search couldn’t load. Use the station list or your device location.");
    try {
      await loadCatalog({ fallback: 1 });
    } catch {
      try {
        addStations(JSON.parse(localStorage.getItem(cachedStationsKey)));
        setText("stationFallbackStatus", "Using the station list saved in this browser.");
      } catch {
        setText("stationFallbackStatus", "The station list couldn’t load. Retry or use your device location.");
      }
      byId("retryStations").hidden = false;
    }
  }
  station.disabled = !stations.size;
  if (stations.size) {
    syncStationInput();
    document.dispatchEvent(new Event("planner-stations-ready"));
  }
}
station.addEventListener("change", (event) => {
  if (event.isTrusted && station.value) rememberStation(station.value);
});
byId("retryStations").addEventListener("click", loadStations);
loadStations();
