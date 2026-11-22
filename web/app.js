"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("preferences");
const station = byId("station");
const budget = byId("budget");
const shown = new Set();
const stations = new Map();
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
let locationPoint;
let locationVersion = 0;
let pickedPlace;
let directionPlace;
let selectedArrival;
const directionRequests = new Map();
let closingTick;
let lastTrainTick;
let tripTick;
let nearbyTick;
let nearbyRequestedAt = -Infinity;
let userMarker;
let pickedMarker;
let placesTimer;
let placesVersion = 0;
let currentDish;
let pickRequest;
let dishFareStart;
let placesFareStart;
let subwayFare;
let transferFare = 0;
const fareTrips = () => byId("countRideHome").checked ? 2 : 1;
const fareKind = (trips) => trips === 2 ? "round-trip" : "one-way";
const communityMenuRequests = new Map();
let walkOk = 15;
let activeWalk = false;
let selectedPickPlace;

let homeRenderVersion = 0;
let dietOptions = [];
let nearbyPlaces = [];

let userCircle;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function element(tag, value, parent) {
  const node = document.createElement(tag);
  node.textContent = value;
  parent.append(node);
  return node;
}

function httpUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function link(parent, label, value) {
  const url = httpUrl(value);
  if (!url) return;
  const anchor = element("a", label, parent);
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
}

async function get(path, params = {}, timeout = 15000, controller = new AbortController()) {
  const url = new URL(path, window.location.href);
  url.search = new URLSearchParams(params);
  const deadline = new DOMException("Request timed out.", "TimeoutError");
  const timer = setTimeout(() => controller.abort(deadline), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || data.message || "Request failed.");
    return data;
  } catch (error) {
    throw controller.signal.aborted ? controller.signal.reason : error;
  } finally {
    clearTimeout(timer);
  }
}

function setText(id, value) {
  byId(id).textContent = value ?? "";
}

function transferNote() {
  return transferFare > 0 ? `subway transfers add ${money.format(transferFare)} each` :
    "one fare covers transfers between subway lines";
}

let plannerWalkDefault = 15;
let plannerFareDefault = true;
function addWalkChoices(config) {
  const defaults = [10, 15, 20, 30];
  const values = Array.isArray(config?.options) ? config.options.filter((value) => defaults.includes(value)) : defaults;
  const options = values.length ? values : defaults;
  let saved;
  try {
    saved = Number(localStorage.getItem("midnight-munch-walk-ok"));
  } catch {
    // Storage can be unavailable in private or restricted contexts.
  }
  plannerWalkDefault = options.includes(config?.default) ? config.default : options.includes(15) ? 15 : options[0];
  walkOk = options.includes(saved) ? saved : plannerWalkDefault;
  const container = byId("walkChoices");
  container.replaceChildren();
  for (const minutes of options) {
    const label = element("label", "", container);
    const input = element("input", "", label);
    input.type = "radio";
    input.name = "walk_ok_min";
    input.value = minutes;
    input.checked = minutes === walkOk;
    input.setAttribute("aria-label", `Walk up to ${minutes} minutes, otherwise take the T`);
    element("span", `${minutes} min`, label);
  }
}

function budgetLabel() {
  budget.setAttribute("aria-valuetext", `${budget.value} dollars`);
  const walkingBothWays = isWalkingPlan() && !(selectedHomeTrip && !walkOnly(selectedHomeTrip) && fareTrips() === 2);
  const includesFare = locationPoint && subwayFare !== undefined && !walkingBothWays;
  const chargedTrips = currentDish && selectedTrip ? displayedFare(currentDish).chargedTrips : fareTrips();
  setText("budgetValue", money.format(Number(budget.value)));
  setText("budgetTitle", walkingBothWays ? "Dish only on foot" : includesFare ? `Dish + ${fareKind(chargedTrips)} T fare` : "Per dish");
  budget.style.setProperty("--budget-fill", `${100 * (budget.value - budget.min) / (budget.max - budget.min)}%`);
  byId("budgetFareHelp").hidden = !includesFare;
  byId("budgetFareHelp").title = `Beyond a ${walkOk} minute walk · ${transferNote()}`;
  setText("budgetFareHelp", includesFare
    ? fareTrips() === 2 ? `${money.format(subwayFare)} each way · ${money.format(2 * subwayFare)} round trip` :
      `${money.format(subwayFare)} one way` : "");
}

function addChoices(id, name, values) {
  for (const value of values) {
    const label = element("label", "", byId(id));
    const input = element("input", "", label);
    input.type = "checkbox";
    input.name = name;
    input.value = value;
    element("span", value[0].toUpperCase() + value.slice(1), label);
  }
}

const travelLimits = [10, 15, 20, 30, 45, 60];
let areaOptions = [];
const checkedValues = (container) => [...container.querySelectorAll("input:checked")].map((input) => input.value).filter(Boolean);
const selectedAreas = () => checkedValues(byId("areaChoices"));
const travelLimit = () => checkedValues(byId("travelChoices"))[0] ?? "";
function travelFilters() {
  const areas = selectedAreas();
  const limit = locationPoint && travelLimit();
  return { ...(areas.length ? { areas: areas.join(",") } : {}), ...(limit ? { max_travel_min: limit } : {}) };
}

function addChip(container, name, value, text, type = "checkbox") {
  const label = element("label", "", container);
  const input = element("input", "", label);
  Object.assign(input, { type, name, value });
  element("span", text, label);
  return input;
}

function updateAreaSummary() {
  const count = selectedAreas().length;
  byId("areaChoices").querySelector('input[value=""]').checked = !count;
  updateFilterSummary();
}

function updateFilterSummary() {
  const active = [walkOk !== plannerWalkDefault ? `${walkOk} min walk` : "",
    travelLimit() ? `${travelLimit()} min to get there` : "",
    ...selectedAreas(), ...checkedValues(byId("cravings")), ...checkedValues(byId("diets")),
    byId("lineFilter").value].filter(Boolean);
  setText("areaSummary", active.map((value) => ` · ${value[0].toUpperCase() + value.slice(1)}`).join(""));
}

form.addEventListener("change", () => queueMicrotask(updateFilterSummary));
document.addEventListener("planner-options-ready", updateFilterSummary);
const filterSheet = byId("filterSheet");
byId("moreFilters").addEventListener("click", () => filterSheet.showModal());
byId("closeFilters").addEventListener("click", () => filterSheet.close());
filterSheet.addEventListener("close", () => byId("moreFilters").focus());

function renderAreaChoices(areas) {
  areaOptions = areas;
  const container = byId("areaChoices");
  container.replaceChildren();
  addChip(container, "areas", "", "Any").checked = true;
  for (const area of areas) addChip(container, "areas", area, area);
  updateAreaSummary();
}

byId("areaChoices").addEventListener("change", (event) => {
  if (!event.target.value) {
    for (const input of byId("areaChoices").querySelectorAll("input")) input.checked = input === event.target;
  } else if (selectedAreas().length > 12) {
    event.target.checked = false;
  }
  updateAreaSummary();
  schedulePlaces();
  scheduleLateStrip();
});

for (const value of ["", ...travelLimits]) {
  addChip(byId("travelChoices"), "max_travel_min", String(value), value ? `${value} min` : "Any", "radio").checked = !value;
}
function setTravelLimit(value) {
  for (const input of byId("travelChoices").querySelectorAll("input")) input.checked = input.value === String(value ?? "");
  updateFilterSummary();
  schedulePlaces();
  scheduleLateStrip();
}
function updateTravelAvailability() {
  byId("travelFilter").disabled = !locationPoint;
  byId("travelHint").hidden = Boolean(locationPoint);
}
byId("travelChoices").addEventListener("change", () => {
  schedulePlaces();
  scheduleLateStrip();
});

function setCuisineOptions(cuisines, placeholder = "Cuisines unavailable (restart the server)") {
  const select = byId("cuisine");
  const names = Array.isArray(cuisines) ? cuisines.filter((name) => typeof name === "string" && name.trim()) : [];
  const first = new Option(names.length ? "Any cuisine" : placeholder, "", true, true);
  first.disabled = !names.length;
  select.replaceChildren(first, ...names.map((name) => new Option(name, name)));
  select.disabled = !names.length;
}

async function loadOptions() {
  byId("retryOptions").hidden = true;
  setText("optionsStatus", "Loading preferences…");
  setCuisineOptions(null, "Loading cuisines…");
  try {
    const options = await get("/api/options");
    if (typeof options.map?.tile_url === "string" && httpUrl(options.map.tile_url)) {
      map.attributionControl.removeAttribution(tiles.options.attribution);
      tiles.options.attribution = options.map.provider === "maptiler"
        ? '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> ' + osmAttribution
        : osmAttribution;
      tiles.options.maxZoom = Number.isInteger(options.map.max_zoom) && options.map.max_zoom > 0
        ? options.map.max_zoom : 19;
      map.attributionControl.addAttribution(tiles.options.attribution);
      if (options.map.provider === "maptiler") mapTilerTiles = options.map.tile_url;
      applyTheme(document.documentElement.dataset.theme, options.map.tile_url);
    }
    budget.min = options.slider.min;
    budget.max = options.slider.max;
    budget.step = options.slider.step;
    budget.value = 12;
    subwayFare = Number.isFinite(options.fare?.subway) ? options.fare.subway : undefined;
    transferFare = options.fare?.transfer ?? 0;
    const defaultFareTrips = options.fare?.trips === 1 ? 1 : 2;
    plannerFareDefault = defaultFareTrips === 2;
    try {
      const saved = localStorage.getItem("midnight-munch-fare-trips");
      byId("countRideHome").checked = saved === null ? defaultFareTrips === 2 : saved !== "1";
    } catch {
      byId("countRideHome").checked = defaultFareTrips === 2;
    }
    addWalkChoices(options.walk_ok);
    budgetLabel();
    dietOptions = options.diets;
    addChoices("diets", "diet", dietOptions);
    addChoices("cravings", "craving", options.cravings);
    for (const line of options.lines) byId("lineFilter").add(new Option(line, line));
    renderAreaChoices(options.areas);
    setCuisineOptions(options.cuisines);
    schedulePlaces();
    updateTravelAvailability();
    byId("filters").disabled = false;
    byId("spin").disabled = false;
    setText("optionsStatus", "");
    scheduleLateStrip();
    document.dispatchEvent(new Event("planner-options-ready"));
  } catch {
    setCuisineOptions();
    setText("optionsStatus", "Spin preferences couldn’t load. Retry to enable Spin. You can still browse nearby places.");
    byId("retryOptions").hidden = false;
  }
}

const map = L.map("map", { zoomAnimation: !reducedMotion, fadeAnimation: !reducedMotion,
  markerZoomAnimation: !reducedMotion, keyboardPanDelta: 80 }).setView([42.351, -71.093], 13);
const mapResize = new ResizeObserver(() => map.invalidateSize({ pan: false, debounceMoveend: true }));
mapResize.observe(byId("map"));
const osmTiles = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
let mapTilerTiles;
const osmAttribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const tiles = L.tileLayer(osmTiles, {
  maxZoom: 19,
  attribution: osmAttribution
}).addTo(map);
tiles.on("tileerror", () => {
  setText("tileStatus", "Some map background tiles couldn’t load.");
});
tiles.on("loading", () => setText("tileStatus", ""));
let configuredTiles = osmTiles;
function applyTheme(theme, tileUrl = configuredTiles) {
  configuredTiles = tileUrl;
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  byId("themeToggle").setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
  const fallback = !mapTilerTiles || !mapTilerTiles.includes("/streets-v2/");
  byId("map").classList.toggle("dark-tile-filter", !light && fallback);
  tiles.setUrl(!light && !fallback ? mapTilerTiles.replace("/streets-v2/", "/streets-v2-dark/") : tileUrl);
}
try {
  applyTheme(localStorage.getItem("midnight-munch-theme"));
} catch {
  applyTheme("dark");
}
byId("themeToggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(theme);
  try { localStorage.setItem("midnight-munch-theme", theme); } catch { /* Theme works without storage. */ }
});

const mobileMap = window.matchMedia("(max-width: 899px)");
const layout = byId("stationKeyboardLayout");
function setMapOpen(open, focus = true) {
  const overlay = mobileMap.matches && open;
  layout.classList.toggle("map-open", overlay);
  layout.querySelector(".night-panel").inert = overlay;
  document.querySelector(".top").inert = overlay;
  byId("mapPanel").inert = mobileMap.matches && !overlay;
  map.invalidateSize({ pan: false });
  if (mobileMap.matches && focus) byId(overlay ? "backFromMap" : "openMap").focus();
}
setMapOpen(false, false);
mobileMap.addEventListener("change", () => setMapOpen(false, false));
byId("openMap").addEventListener("click", () => setMapOpen(true));
byId("backFromMap").addEventListener("click", () => setMapOpen(false));
byId("mapPanel").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && layout.classList.contains("map-open")) setMapOpen(false);
});

const placeMarkers = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const hasCoordinates = (place) => Number.isFinite(place?.lat) && Number.isFinite(place?.lng);
const coordinates = (place) => [place.lat, place.lng];

const iconPaths = {
  moon: "M20.8 14.1A9 9 0 0 1 9.9 3.2 9 9 0 1 0 20.8 14.1Z",
  person: "M20 21v-2a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v2m12-13a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  chevron: "m6 9 6 6 6-6",
  check: "m5 12 4 4L19 6",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 6v6l4 2",
  bus: "M5 18h14V6a3 3 0 0 0-3-3H8a3 3 0 0 0-3 3v12Zm0-8h14M8 15h.01M16 15h.01M7 18v3m10-3v3",
  train: "M6 19h12l2-2V6a3 3 0 0 0-3-3H7a3 3 0 0 0-3 3v11l2 2Zm-2-8h16M9 15h.01M15 15h.01M8 19l-2 3m10-3 2 3",
  walk: "M13 4a1 1 0 1 0 0-2 1 1 0 0 0 0 2ZM8 11l3-4 3 1 2 4 3 1m-7-5-2 8-4 5m5-7 5 3 1 4",
  pin: "M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Zm-5 0a3 3 0 1 0-6 0 3 3 0 0 0 6 0Z",
  close: "m6 6 12 12M18 6 6 18",
  back: "m11 5-7 7 7 7m-7-7h16",
  refresh: "M20 7v5h-5m5 0a8 8 0 1 0-2 6",
  eat: "M5 3v7a3 3 0 0 0 6 0V3M8 3v18m11-18c-3 2-4 6-4 10h4m0-10v18"
};

function makeIcon(name) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("ui-icon");
  const path = document.createElementNS(namespace, "path");
  path.setAttribute("d", iconPaths[name]);
  svg.append(path);
  return svg;
}

for (const placeholder of document.querySelectorAll("[data-icon]")) {
  const icon = makeIcon(placeholder.dataset.icon);
  icon.classList.add(...placeholder.classList);
  placeholder.replaceWith(icon);
}

function iconText(symbol) {
  const node = document.createElement("span");
  node.textContent = typeof symbol === "string" ? symbol : "";
  return node;
}

// Leaflet 1.1.1 parses divIcon.html as a string. Attach content as DOM instead.
const MarkerIcon = L.DivIcon.extend({
  createIcon(oldIcon) {
    const icon = L.DivIcon.prototype.createIcon.call(this, oldIcon);
    icon.append(this.options.content);
    return icon;
  }
});

function marker(place, style, symbol) {
  const label = typeof place.name === "string" ? place.name : "";
  const pin = L.marker(coordinates(place), {
    icon: new MarkerIcon({ className: `pin ${style}`, html: "", content: symbol instanceof Element ? symbol : iconText(symbol),
      iconSize: [32, 32], popupAnchor: [0, -14] }),
    title: label,
    alt: label,
    zIndexOffset: style.includes("selected") ? 1000 : style === "user" ? 500 : 0
  });
  pin.on("add", () => pin.getElement().setAttribute("aria-label", label));
  return pin;
}

function showPriceTier(place, parent) {
  if (!["$", "$$", "$$$"].includes(place.price_tier)) return;
  const estimated = place.price_tier_estimated === true;
  const chip = element("span", estimated ? `${place.price_tier} est.` : place.price_tier, parent);
  chip.className = "tier-chip";
  chip.title = estimated ? "Estimated restaurant price tier" : "Restaurant price tier";
}

function placeMarker(place, selected = false) {
  const popup = document.createElement("div");
  element("strong", place.name, popup);
  popup.append(stampIcon(place));
  const noMenu = place.has_menu === false;
  element("p", placeCloses(place) || place.hours_text || "Hours unavailable", popup);
  showPriceTier(place, popup);
  if (noMenu) element("p", "No public menu", popup);
  if (Number.isFinite(place.from_price)) element("p", `From ${money.format(place.from_price)}`, popup);
  const actions = element("div", "", popup);
  actions.className = "links";
  actions.className += " backup";
  link(actions, "Open in Maps", mapsLink(place));
  const button = element("button", "Directions", actions);
  button.type = "button";
  button.addEventListener("click", () => {
    planPlace(place);
  });
  const openLate = place.open_late === true;
  const picked = selected;
  const style = [selected && "selected", noMenu && "no-menu", place.open_now === false && "closed-place", picked ? "picked" : openLate && "late"].filter(Boolean).join(" ");
  const stamp = stampIcon(place);
  let icon = stamp;
  if (picked || openLate) {
    icon = iconText(picked ? "★" : "L");
    icon.className = "pin-symbol";
    stamp.classList.add("pin-stamp");
    icon.append(stamp);
  }
  const pin = marker(place, style, icon);
  pin.on("add", () => pin.getElement().setAttribute("aria-label",
    [place.name, place.open_now === false && placeCloses(place), noMenu && "No public menu", picked && "Your pick", openLate && "Open past midnight", stamp.getAttribute("aria-label")].filter(Boolean).join(", ")));
  pin.on("click", () => {
    if (placingStart) {
      chooseMapStart(place);
      return;
    }
    if (pickedPlace?.id !== place.id) planPlace(place);
    map.openPopup(popup, coordinates(place), { offset: [0, -14], autoPan: false });
  });
  return pin;
}

function showListPrice(place, parent, start = placesFareStart) {
  const hasFare = subwayFare !== undefined && locationPoint && locationPoint === start &&
    Number.isFinite(place.fare) && Number.isFinite(place.from_total);
  const price = money.format(place.from_price);
  const suffix = !hasFare ? "" : place.fare > 0 ? ` · +${money.format(place.fare)} T` : " · walkable";
  const label = element("small", `${hasFare ? "from" : "From"} ${price}${suffix}`, parent);
  label.className = "from-price";
  if (hasFare) {
    const each = Number.isFinite(place.fare_each) && place.fare_trips === 2
      ? ` (${money.format(place.fare_each)} each way)` : "";
    label.title = `Total: ${money.format(place.from_total)}${place.fare > 0
      ? ` · ${fareKind(place.fare_trips)} T fare${each} · ${transferNote()}` : ""}`;
  }
}

const communityDishLabel = (item) => `Community dish · ${item.votes} reports`;

function addPlaceCommunity(place, parent) {
  if (!(place.community_dishes > 0)) return;
  let entry = communityMenuRequests.get(place.id);
  if (!entry || Date.now() - entry.time >= 30000) {
    const request = get("/api/menu", { place_id: place.id }).then(({ items }) => items).catch(() => []);
    entry = { time: Date.now(), request };
    communityMenuRequests.set(place.id, entry);
  }
  entry.request.then((menu) => {
    if (!parent.isConnected || parent.querySelector(".community-chip")) return;
    const dishes = menu.filter((item) => item.community === true && Number.isInteger(item.votes));
    const communityDish = dishes.find((item) => item.price === place.from_price) ?? dishes[0];
    if (!communityDish) return;
    element("span", communityDishLabel(communityDish), parent).className = "community-chip";
    element("small", `${communityDish.name} · price reported by diners`, parent);
  });
}

const isBrowseSuggestion = (place) => (!locationPoint || place.reachable !== false) &&
  (!byId("favoritesOnly").checked || isFavorite(place));

function renderPlaces() {
  placeMarkers.clearLayers();
  const list = byId("placeList");
  const activeId = list.contains(document.activeElement) ? document.activeElement.dataset.placeId : null;
  list.replaceChildren();
  const origin = locationPoint || map.getCenter();
  for (const place of nearbyPlaces) {
    if (hasCoordinates(place) && place.id !== pickedPlace?.id) placeMarker(place).addTo(placeMarkers);
    if (!isBrowseSuggestion(place)) continue;
    const row = element("li", "", list);
    row.className = "browse-place-row";
    const button = element("button", "", row);
    button.type = "button";
    button.className = place.open_now === false ? "restaurant closed-place" : "restaurant";
    button.dataset.placeId = place.id;
    button.setAttribute("aria-pressed", String(place.id === pickedPlace?.id));
    button.append(makeIcon("pin"));
    const info = element("span", "", button);
    const name = element("strong", place.name + " ", info);
    name.append(stampIcon(place));
    const miles = hasCoordinates(place) ? map.distance(origin, coordinates(place)) / 1609.344 : null;
    element("small", [place.area, miles === null ? null : `${miles.toFixed(1)} mi`].filter(Boolean).join(" · "), info);
    if (locationPoint && placesFareStart === locationPoint) {
      const estimated = Number.isFinite(place.travel_estimate_min);
      const minutes = estimated ? place.travel_estimate_min : place.walk_min;
      if (Number.isFinite(minutes)) element("small", travelTimeLabel(minutes, estimated && place.mode !== "walk", estimated), info);
    }
    const closing = placeCloses(place);
    if (closing) element("small", closing, info);
    showUnverifiedHours(place, info);
    const pricing = element("span", "", button);
    pricing.className = "place-pricing";
    showPriceTier(place, pricing);
    if (place.has_menu === false) element("small", "No public menu", pricing);
    if (Number.isFinite(place.from_price)) showListPrice(place, pricing);
    addPlaceCommunity(place, info);
    addFavorite(place, row);
    button.addEventListener("click", () => planPlace(place));
    if (String(place.id) === activeId) button.focus({ preventScroll: true });
  }
}

async function loadPlaces(version) {
  const start = locationPoint;
  const center = start || map.getCenter();
  const bounds = map.getBounds();
  const radius = Math.max(map.distance(center, bounds.getNorthEast()), map.distance(center, bounds.getSouthWest())) / 1000;
  const params = {
    budget: budget.value, q: byId("search").value.trim(),
    walk_ok_min: walkOk, trips: fareTrips(), min_minutes: stayMinutes(),
    ...travelFilters(), cuisine: byId("cuisine").value, menus: byId("menus").value, browse: byId("browseAll").checked ? 1 : 0
  };
  if (start) Object.assign(params, { lat: start.lat, lng: start.lng, radius_km: radius });
  byId("retryPlaces").hidden = true;
  setText("mapStatus", nearbyPlaces.length ? "Updating nearby places…" : "Loading places around the map…");
  try {
    const data = await get("/api/places", withDemoTime(params));
    if (version !== placesVersion) return;
    placesFareStart = start;
    if (data.now_iso) data.places.forEach((place) => { place.now_iso = data.now_iso; });
    // Keep the viewport local without telling the server the map center is a real start.
    nearbyPlaces = start ? data.places : data.places
      .filter((place) => hasCoordinates(place) && map.distance(center, coordinates(place)) <= radius * 1000)
      .sort((a, b) => map.distance(center, coordinates(a)) - map.distance(center, coordinates(b)));
    renderPlaces();
    renderSelectedDetails();
    const count = nearbyPlaces.filter(isBrowseSuggestion).length;
    const empty = byId("favoritesOnly").checked ? "No favorites match these filters." : params.q
      ? "No places match your search in this map area. Clear the search or change your filters."
      : "No places match these filters in this map area. Change your filters or move the map.";
    setText("mapStatus", count
      ? `${count} ${count === 1 ? "place" : "places"}${locationPoint ? "" : " around the map center. Set a start for distances from your start."}`
      : empty);
  } catch {
    if (version !== placesVersion) return;
    setText("mapStatus", nearbyPlaces.length
      ? "Places couldn’t refresh. Showing previous results. Try again."
      : "Nearby places couldn’t load. Try again.");
    byId("retryPlaces").hidden = false;
  }
}

let lateStripTimer;
let lateStripRequest;

function showUnverifiedHours(place, parent) {
  if (place.hours_unverified === true || place.open_now === null) element("span", "Hours unverified", parent).className = "hours-chip";
}

function placeCloses(place) {
  if (place.open_now === false) {
    const opens = place.opens_at_text || formatTime(place.opens_at_iso);
    return opens ? `Closed · opens ${opens}` : "Closed";
  }
  if (place.closes_at_text === "Open 24 hours") return "Open 24 hours";
  const time = place.closes_at_text || formatTime(place.open_until_iso);
  return time ? `Closes ${time}` : "";
}

function scheduleLateStrip() {
  clearTimeout(lateStripTimer);
  lateStripRequest?.abort();
  lateStripTimer = setTimeout(loadLateStrip, 200);
}

function travelTimeLabel(minutes, rides = false, estimated = false) {
  return `${estimated ? "about " : ""}${minutes} min ${rides ? "by T" : "walk"}${estimated ? " (estimate)" : ""}`;
}

async function loadLateStrip() {
  const list = byId("lateList");
  offerFix("lateFix");
  if (byId("planSetup").hidden || byId("planView").hidden) return;
  if (!locationPoint) {
    list.replaceChildren();
    setText("lateStripStatus", "Choose a start to find late kitchens you can reach.");
    return;
  }
  const point = locationPoint;
  const stay = stayMinutes();
  const limit = walkOk;
  const request = new AbortController();
  lateStripRequest = request;
  setText("lateStripStatus", "Checking late kitchens…");
  list.replaceChildren();
  try {
    const data = await get("/api/places", withDemoTime({
      lat: point.lat, lng: point.lng, radius_km: 8, late: 1, menus: "with", budget: budget.value, walk_ok_min: limit, trips: fareTrips(), min_minutes: stay, ...travelFilters()
    }), 15000, request);
    const verified = data.places.filter((place) => place.late_eligible === true && place.open_now !== false && place.reachable !== false);
    const check = async (place) => {
      if (Number.isFinite(place.travel_min)) return { place, minutes: place.travel_min,
        time: travelTimeLabel(place.travel_min, place.mode !== "walk", place.travel_verified === false) };
      if ((place.mode === "walk" || data.transit_running === false) && Number.isFinite(place.walk_min)) {
        return { place, time: travelTimeLabel(place.walk_min), minutes: place.walk_min };
      }
      const trip = await get("/api/trip", withDemoTime({ ...tripParams(point, place), late: 1, trips: fareTrips() }), 15000, request);
      const best = trip.itineraries?.[0];
      if (!best) return null;
      const minutes = Math.max(best.summary.duration_min, Math.ceil(
        (Date.parse(best.summary.arrive_iso) - Date.parse(trip.now_iso)) / 60000
      ) || 0);
      const estimated = includesEstimatedWalk(best);
      return { place, time: travelTimeLabel(minutes, rideLegs(best).length > 0, estimated), minutes };
    };
    const feasible = [];
    for (let index = 0; index < verified.length && feasible.length < 4; index += 4) {
      const group = await Promise.allSettled(verified.slice(index, index + 4).map(check));
      if (request.signal.aborted) return;
      for (const entry of group) {
        if (entry.status !== "fulfilled" || !entry.value) continue;
        const { place, minutes } = entry.value;
        if (place.closes_at_text === "Open 24 hours" || place.closes_in_min >= stay + minutes) {
          feasible.push(entry.value);
        }
      }
    }
    for (const { place, time } of feasible.slice(0, 4)) {
      const row = element("button", "", element("li", "", list));
      row.type = "button";
      row.className = "restaurant";
      element("strong", place.name, row);
      const info = element("span", "", row);
      info.className = "strip-info";
      const closing = placeCloses(place);
      const meta = element("small", "", info);
      showPriceTier(place, meta);
      if (closing) meta.append(meta.childNodes.length ? ` · ${closing}` : closing);
      const pricing = element("div", "", info);
      if (Number.isFinite(place.from_price)) element("span", `From ${money.format(place.from_price)}`, pricing);
      if (pricing.childNodes.length) pricing.append(" · ");
      pricing.append(place.mode === "walk" ? `walkable · ${time}` : time);
      addPlaceCommunity(place, info);
      row.addEventListener("click", () => planPlace({ ...place, now_iso: data.now_iso }, true));
    }
    if (feasible.length) {
      setText("lateStripStatus", "");
    } else if (data.transit_running === false) {
      setText("lateStripStatus", `No trains or buses are running. Nothing open within a ${limit} minute walk.`);
    } else {
      const hasClosingData = data.places.some((place) => typeof place.late_eligible === "boolean");
      setText("lateStripStatus", data.places.length && !hasClosingData
        ? "Closing times aren’t available to verify a late bite here."
        : "No late bites in budget are confirmed for your stay.");
      if (data.places.length && !hasClosingData) {
        offerFix("lateFix", "See all places", () => setNightTab("browse", true));
      } else if (byId("lateMode").checked) {
        offerFix("lateFix", "Try Anytime", () => { setLate(false); spin(false); });
      } else if (Number(budget.value) < Number(budget.max)) {
        offerFix("lateFix", `Raise budget to $${Number(budget.value) + Number(budget.step)}`, raiseBudget);
      }
    }
  } catch {
    if (request.signal.aborted) return;
    setText("lateStripStatus", "Late places couldn’t load.");
    offerFix("lateFix", "Retry", scheduleLateStrip);
  }
}

function schedulePlaces() {
  clearTimeout(placesTimer);
  const version = ++placesVersion;
  placesTimer = setTimeout(() => loadPlaces(version), 200);
}

function setLocation(point) {
  locationPoint = point;
  budgetLabel();
  if (currentDish) renderDishPrice(currentDish);
  renderPlaces();
  updateTravelAvailability();
  renderOrigin();
  if (point) {
    map.setView(coordinates(point), Math.max(map.getZoom(), 13), { animate: !reducedMotion });
  }
  setText("locationStatus", point && !stations.has(point.id) ? `Starting from ${point.name}.` : "");
  schedulePlaces();
  scheduleLateStrip();
  document.dispatchEvent(new Event("start-location-changed"));
  if (directionPlace) planPlace(directionPlace, selectedPickPlace?.late ?? byId("lateMode").checked);
}

station.addEventListener("change", () => {
  setStartMode(false);
  locationVersion++;
  byId("locate").disabled = false;
  setLocation(stations.get(station.value));
});
byId("locate").addEventListener("click", () => {
  setStartMode(false);
  if (!window.isSecureContext || !navigator.geolocation) {
    setText("locationStatus", "Device location is unavailable. Use localhost or HTTPS, or choose a T station.");
    return;
  }
  const version = ++locationVersion;
  byId("locate").disabled = true;
  setText("locationStatus", "Finding your location…");
  navigator.geolocation.getCurrentPosition(({ coords }) => {
    if (version !== locationVersion) return;
    byId("locate").disabled = false;
    station.value = "";
    setLocation({ lat: coords.latitude, lng: coords.longitude, name: "Your location" });
    setText("locationStatus", "Using your device location.");
  }, (error) => {
    if (version !== locationVersion) return;
    byId("locate").disabled = false;
    const reason = error.code === 1 ? "Location permission was denied." : "Your device location couldn’t be found.";
    setText("locationStatus", `${reason} Choose a T station or try again.`);
  }, { timeout: 10000 });
});

let placingStart = false;

function setStartMode(enabled) {
  placingStart = enabled;
  byId("setStart").setAttribute("aria-pressed", String(enabled));
  byId("map").classList.toggle("start-mode", enabled);
  setText("mapStartStatus", enabled ? "Click the map or press Enter to use its center." : "");
}

function chooseMapStart(point) {
  locationVersion++;
  byId("locate").disabled = false;
  station.value = "";
  setStartMode(false);
  setLocation({ lat: point.lat, lng: point.lng, name: "your map starting point" });
}

byId("setStart").addEventListener("click", () => {
  setStartMode(!placingStart);
  if (placingStart) byId("map").focus();
});
map.on("click", ({ latlng }) => {
  if (placingStart) chooseMapStart(latlng);
});
byId("map").addEventListener("keydown", (event) => {
  if (!placingStart) return;
  if (event.key === "Enter" && event.target === byId("map")) {
    event.preventDefault();
    chooseMapStart(map.getCenter());
  } else if (event.key === "Escape") {
    setStartMode(false);
    byId("setStart").focus();
  }
});

function renderOrigin() {
  userMarker?.remove();
  userCircle?.remove();
  if (!locationPoint) return;
  userCircle = L.circle(coordinates(locationPoint), {
    radius: 60.96, color: "#224b3b", fillOpacity: .13, interactive: false
  }).addTo(map);
  const avatar = iconText("");
  avatar.className = "avatar";
  paintAvatar(avatar);
  userMarker = marker(locationPoint, "user", avatar).addTo(map);
}

function renderSelectedMarker() {
  pickedMarker?.remove();
  if (hasCoordinates(pickedPlace)) pickedMarker = placeMarker(pickedPlace, true).addTo(map);
}

function highlight(place) {
  pickedPlace = place;
  renderSelectedMarker();
  renderPlaces();
  if (hasCoordinates(place) && !map.getBounds().contains(coordinates(place))) {
    map.panTo(coordinates(place), { animate: !reducedMotion });
  }
}

function setNightTab(tab, focus = false) {
  for (const name of ["plan", "browse"]) {
    const active = name === tab;
    const button = byId(`${name}Tab`);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    byId(`${name}View`).hidden = !active;
    if (focus && active) button.focus();
  }
  document.dispatchEvent(new Event("night-tab-changed"));
  if (tab === "browse") {
    pickRequest?.abort();
    lateStripRequest?.abort();
    schedulePlaces();
  } else {
    scheduleLateStrip();
    if (directionPlace && !byId("selectedDetails").hidden) requestTimingRefresh("visible");
  }
}

for (const name of ["plan", "browse"]) {
  const button = byId(`${name}Tab`);
  button.addEventListener("click", () => setNightTab(name));
  button.addEventListener("keydown", (event) => {
    const next = { ArrowLeft: name === "plan" ? "browse" : "plan", ArrowRight: name === "plan" ? "browse" : "plan",
      Home: "plan", End: "browse" }[event.key];
    if (!next) return;
    event.preventDefault();
    setNightTab(next, true);
  });
}
byId("seeAll").addEventListener("click", () => {
  setNightTab("browse");
  byId("search").focus();
});

const stayMinutes = () => Number(form.querySelector('[name="stay"]:checked').value);

function setStay(minutes) {
  form.querySelector(`[name="stay"][value="${minutes}"]`).checked = true;
  scheduleLateStrip();
  requestTimingRefresh("stay");
}

function setLate(late) {
  byId("lateMode").checked = late;
  byId("lateButton").setAttribute("aria-pressed", String(late));
  byId("anytimeButton").setAttribute("aria-pressed", String(!late));
  setText("spin", late ? "Find my late-night bite" : "Find a bite");
  byId("lateSuggestion").hidden = late;
}
byId("lateButton").addEventListener("click", () => setLate(true));
byId("anytimeButton").addEventListener("click", () => setLate(false));

function selectPlace(place, planRoute = true) {
  if (currentDish?.place.id !== place.id) {
    currentDish = null;
    byId("result").hidden = true;
    byId("dishBadges").replaceChildren();
    showUnverifiedHours(place, byId("dishBadges"));
    setText("communityPrice", "");
    showClosing(place, place.now_iso ? responseClock(place.now_iso) : planningNow);
    showAboutPlace(place);
  }
  byId("heroReport").hidden = !currentDish;
  highlight(place);
  setMapOpen(false, false);
  setNightTab("plan");
  byId("planSetup").hidden = true;
  byId("selectedDetails").hidden = false;
  byId("aboutDisclosure").open = byId("nearbyDisclosure").open = false;
  renderSelectedDetails();
  byId("planScroll").scrollTop = 0;
  const work = planRoute ? showDirections(place) : undefined;
  if (byId("nightLoading").hidden) byId("nightHeading").focus({ preventScroll: true });
  return work;
}

function changePlan() {
  pickRequest?.abort();
  byId("nightLoading").hidden = true;
  activeWalk = false;
  selectedPickPlace = undefined;
  byId("selectedDetails").hidden = true;
  byId("planSetup").hidden = false;
  byId("result").hidden = true;
  setText("resultStatus", "");
  offerFix("resultRetry");
  currentDish = directionPlace = selectedTrip = null;
  closingTick = lastTrainTick = tripTick = nearbyTick = null;
  cancelDirections();
  budgetLabel();
  routeLayer.clearLayers();
  map.closePopup();
  highlight(null);
  scheduleLateStrip();
  byId("planScroll").scrollTop = 0;
  byId("planHeading").focus({ preventScroll: true });
}
byId("closeDetails").addEventListener("click", changePlan);

function showAboutPlace(place, dish) {
  const details = byId("dishDetails");
  details.replaceChildren();
  for (const text of [place.address, place.area, place.stop_line, place.hours_text || "Hours unavailable"]) {
    if (text) element("p", text, details);
  }
  if (dish) {
    if (dish.tags.length) element("p", dish.tags.join(" · "), details).className = "hint";
    element("p", "Diet tags come from menu wording. Check with the restaurant.", details).className = "hint";
    link(details, "Menu source", dish.source_url);
  } else {
    link(details, "Restaurant website", place.website_url);
  }
}

function showDish(dish, clock, start) {
  const { place } = dish;
  currentDish = dish;
  dishFareStart = start;
  activeWalk = dish.mode === "walk";
  selectedTrip = selectedHomeTrip = null;
  tripFares = {};
  homeModeOverride = undefined;
  budgetLabel();
  rememberDish(dish);
  setText("dish", dish.name);
  setText("dishRestaurant", place.name);
  setText("dishTravel", Number.isFinite(dish.travel_min) ? travelTimeLabel(dish.travel_min, dish.mode !== "walk", dish.travel_verified === false) : "");
  const note = typeof dish.price_note === "string" ? dish.price_note.trim() : "";
  byId("price").title = /^sizes:/i.test(note) ? note : "";
  renderDishPrice(dish);
  const badges = byId("dishBadges");
  badges.replaceChildren();
  if (dish.community === true) element("span", communityDishLabel(dish), badges).className = "community-chip";
  showUnverifiedHours(place, badges);
  if (place.open_late === true) element("span", "Open late", badges);
  else if (place.open_now === true) element("span", "Open now", badges);
  if (place.cuisine) element("span", place.cuisine, badges);
  if (dish.price_status === "verify") element("span", "Price may vary", badges);
  for (const tag of dish.tags.filter((tag) => dietOptions.includes(tag)).slice(0, 2)) element("span", tag, badges);
  showAboutPlace(place, dish);
  if (note) element("p", note, byId("dishDetails")).className = "hint";
  showClosing(place, clock);
  byId("result").hidden = false;
  return selectPlace(place);
}

function offerFix(id, label, action) {
  const button = byId(id);
  button.hidden = !label;
  button.textContent = label ?? "";
  button.onclick = action ?? null;
}

const planPrimary = byId("planPrimary");
function measurePlanPrimary() {
  const height = planPrimary.getBoundingClientRect().height;
  if (height) byId("planScroll").style.setProperty("--plan-primary-height", `${height}px`);
}
new ResizeObserver(measurePlanPrimary).observe(planPrimary);
window.addEventListener("resize", () => requestAnimationFrame(revealOpeningFeedback));

function revealOpeningFeedback() {
  if (!Object.hasOwn(byId("planSetup").dataset, "browseOpening") || byId("planView").hidden) return;
  measurePlanPrimary();
  byId("planFeedback").scrollIntoView({ block: "nearest" });
  const button = byId("planFix");
  if (button.getBoundingClientRect().bottom > planPrimary.getBoundingClientRect().top) {
    button.scrollIntoView({ block: "end" });
  }
}

function raiseBudget() {
  budget.value = Number(budget.value) + Number(budget.step);
  budget.dispatchEvent(new Event("input"));
}

function planPlace(place, late = byId("lateMode").checked) {
  if (pickRequest) return;
  activeWalk = false;
  currentDish = null;
  selectedPickPlace = { place, late };
  cancelDirections();
  routeLayer.clearLayers();
  directionPlace = null;
  closingTick = lastTrainTick = tripTick = nearbyTick = null;
  byId("directions").hidden = true;
  selectPlace(place, false);
  showLastTrain(null, Date.now, "Checking schedules…");
  spin(false, selectedPickPlace);
}

function updateLoading(stage) {
  const labels = ["Open kitchens", "Trains there", "Last T home"];
  const announcements = ["Checking open kitchens", "Kitchen found. Checking the trip there.",
    "Outbound route ready. Checking the way home.", "Night plan ready."];
  setText("loadingAnnouncement", announcements[stage]);
  for (const [index, step] of [...byId("loadingSteps").children].entries()) {
    step.dataset.progress = index < stage ? "done" : index === stage ? "current" : "pending";
    step.replaceChildren();
    if (index < stage) step.append(makeIcon("check"));
    else {
      const indicator = element("span", "", step);
      indicator.className = "loading-indicator";
      indicator.setAttribute("aria-hidden", "true");
    }
    step.append(labels[index]);
    const status = element("span", ` · ${step.dataset.progress}`, step);
    status.className = "sr-only";
    if (index === stage) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  }
}

byId("cancelLoading").addEventListener("click", changePlan);

async function spin(reroll, specific = reroll ? selectedPickPlace : undefined, initialProgress) {
  if (pickRequest || (!specific && byId("spin").disabled)) return;
  const start = locationPoint;
  let feedback = byId("selectedDetails").hidden ? "status" : "resultStatus";
  offerFix("planFix");
  offerFix("resultRetry");
  byId("nextOpening").hidden = byId("openingSuggestion").hidden = byId("resultOpeningSuggestion").hidden = true;
  delete byId("planSetup").dataset.browseOpening;
  if (!start && !specific) {
    if (feedback === "resultStatus") changePlan();
    setText("status", "Choose where you are first.");
    (station.disabled ? byId("locate") : byId("stationInputGroup").hidden ? station : byId("stationSearch")).focus();
    return;
  }
  if (!reroll) shown.clear();
  if (!specific) selectedPickPlace = undefined;
  const fields = new FormData(form);
  const params = { budget: budget.value,
    min_minutes: stayMinutes(), walk_ok_min: walkOk, trips: fareTrips(), ...travelFilters() };
  if (start) Object.assign(params, { lat: start.lat, lng: start.lng });
  if (specific?.late ?? fields.has("late")) params.late = "1";
  if (specific) params.place_id = specific.place.id;
  for (const name of ["diet", "craving", "line"]) {
    const value = fields.getAll(name).join(",");
    if (value) params[name] = value;
  }
  if (reroll && shown.size) params.exclude = [...shown].join(",");
  const request = new AbortController();
  const preferencesDisabled = byId("filters").disabled;
  pickRequest = request;
  byId("spin").disabled = byId("reroll").disabled = true;
  const progressTarget = feedback === "status" ? "pickProgress" : feedback;
  setText(feedback, "");
  setText(progressTarget, initialProgress ?? (params.late ? "Finding late kitchens…" : "Finding a bite that fits your night…"));
  const loading = byId("nightLoading");
  const previousScroll = byId("planScroll").scrollTop;
  let candidate = false;
  updateLoading(0);
  loading.hidden = false;
  byId("planScroll").scrollTop = 0;
  byId("loadingHeading").focus({ preventScroll: true });
  try {
    const { dish, message, relaxed, now_iso, t_timeout, next_open, opens_at_iso, no_service, suggestion } = await get("/api/pick", withDemoTime(params), 30000, request);
    if (request.signal.aborted) return;
    if (start !== locationPoint) {
      setText(feedback, "Start changed. Find a bite again to use it.");
      return;
    }
    if (t_timeout) {
      setText(feedback, "Train times are loading slowly. Try again in a moment.");
      offerFix(feedback === "status" ? "planFix" : "resultRetry", "Retry", () => spin(reroll, specific));
      return;
    }
    const clock = responseClock(now_iso);
    if (dish && dish.place.open_now !== false) {
      shown.add(dish.item_id);
      setText("status", "");
      setText("resultStatus", message || (relaxed?.length ? `Adjusted filters: ${relaxed.map((key) => key.replaceAll("_", " ")).join(", ")}.` : ""));
      candidate = true;
      updateLoading(1);
      await showDish(dish, clock, start);
      if (!request.signal.aborted && selectedHomeTrip) updateLoading(3);
    } else {
      const closed = Boolean(opens_at_iso || specific?.place.open_now === false ||
        dish?.place.open_now === false || /\bis closed right now\b/i.test(message ?? ""));
      const tooSoon = /\btoo soon for a\b/i.test(message ?? "");
      const travelNoFit = /^Nothing within \d+ minutes of you right now\b/i.test(message ?? "");
      const nothingOpen = !specific && (no_service || next_open || /^Nothing is open\b/i.test(message ?? ""));
      if ((nothingOpen || !specific && travelNoFit) && feedback === "resultStatus") {
        changePlan();
        feedback = "status";
      }
      setText(feedback, message || (closed ? "This restaurant is closed right now." : "No dishes matched your plan."));
      if (nothingOpen) {
        const opens = next_open?.opens_at_text || formatTime(next_open?.opens_at_iso);
        byId("nextOpening").hidden = !next_open?.name || !opens;
        if (next_open?.name && opens) setText("nextOpening", `Closest opening: ${next_open.name} at ${opens}`);
        byId("openingSuggestion").hidden = !suggestion;
        setText("openingSuggestion", suggestion ?? "");
        offerFix("planFix", "Browse places", () => setNightTab("browse", true));
        byId("planSetup").dataset.browseOpening = "";
        requestAnimationFrame(revealOpeningFeedback);
        return;
      }
      if (travelNoFit) {
        const next = travelLimits.find((minutes) => minutes > Number(params.max_travel_min));
        offerFix(feedback === "status" ? "planFix" : "resultRetry", next ? `Try ${next} minutes` : "Try any travel time", () => {
          setTravelLimit(next);
          spin(false, specific);
        });
        return;
      }
      if (specific && !currentDish && (closed || tooSoon || no_service)) {
        setText("resultOpeningSuggestion", suggestion ?? "");
        byId("resultOpeningSuggestion").hidden = !suggestion;
        if (closed) {
          setText("closing", placeCloses({ ...specific.place, open_now: false, opens_at_iso: opens_at_iso ?? specific.place.opens_at_iso }));
        } else if (tooSoon) {
          setText("closing", "Closes too soon");
        }
        showLastTrain(null, clock, "No trip planned");
        return;
      }
      if (specific && !currentDish) {
        activeWalk = specific.place.mode === "walk";
        showClosing(specific.place, clock);
        showDirections(specific.place);
      }
      if (feedback === "status") {
        if (/budget|fare|within \$/i.test(message ?? "") && Number(budget.value) < Number(budget.max)) {
          offerFix("planFix", `Raise budget to $${Number(budget.value) + Number(budget.step)}`, () => { raiseBudget(); spin(false); });
        } else if (byId("lateMode").checked && stayMinutes() > 30) {
          offerFix("planFix", "Try staying 30 min", () => { setStay(30); spin(false); });
        } else if (byId("lateMode").checked) {
          offerFix("planFix", "Try Anytime", () => { setLate(false); spin(false); });
        }
      }
    }
  } catch (error) {
    if (request.signal.aborted && error.name === "AbortError") {
      setText(feedback, "");
      return;
    }
    setText(feedback, error.message || "A dish couldn’t be picked right now.");
    if (feedback === "status") offerFix("planFix", "Retry", () => spin(reroll));
    else {
      offerFix("resultRetry", "Retry", () => spin(reroll, specific));
      if (specific && !currentDish && specific.place.open_now !== false) showDirections(specific.place);
    }
  } finally {
    loading.hidden = true;
    if (!request.signal.aborted && byId("browseView").hidden) {
      if (!candidate) byId("planScroll").scrollTop = previousScroll;
      if (!byId("selectedDetails").hidden) byId("nightHeading").focus({ preventScroll: true });
      else if (document.activeElement === document.body) byId("planHeading").focus({ preventScroll: true });
    }
    setText("pickProgress", "");
    pickRequest = null;
    byId("spin").disabled = preferencesDisabled && byId("filters").disabled;
    byId("reroll").disabled = false;
  }
}

function replaceRemovedDish(placeId, itemId) {
  if (currentDish?.item_id !== itemId || currentDish.place.id !== placeId) return;
  shown.add(itemId);
  currentDish = null;
  byId("result").hidden = true;
  byId("heroReport").hidden = true;
  byId("dishBadges").replaceChildren();
  setText("communityPrice", "");
  spin(true, selectedPickPlace, "Removed after reports. Finding another.");
}

const bostonTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "numeric", minute: "2-digit"
});
const bostonHour = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "numeric", hourCycle: "h23"
});
const demoTime = new URLSearchParams(window.location.search).get("now");
const demoParts = /^(?:\d{4}-\d{2}-\d{2}T)?([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$/.exec(demoTime);
const pageOpened = Date.now();
const pageMonotonic = performance.now();

function elapsedClock(startWall, startMono) {
  let elapsed = 0;
  return () => {
    elapsed = Math.max(elapsed, Date.now() - startWall, performance.now() - startMono);
    return elapsed;
  };
}

const pageElapsed = elapsedClock(pageOpened, pageMonotonic);

function formatTime(iso) {
  const time = Date.parse(iso);
  return Number.isFinite(time) ? bostonTime.format(time).replace(/\s/g, " ") : "";
}

function withDemoTime(params, now = planningNow()) {
  if (demoEpoch !== null) return { ...params, now: new Date(now).toISOString() };
  return demoTime === null ? params : { ...params, now: demoTime };
}

function responseClock(iso) {
  if (demoEpoch !== null) return planningNow;
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return Date.now;
  const elapsed = elapsedClock(Date.now(), performance.now());
  return () => time + elapsed();
}

const nightTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit"
});
const initialHour = demoParts ? Number(demoParts[1]) : Number(bostonHour.format(Date.now()));
setLate(initialHour >= 21 || initialHour < 5);
const demoInitial = demoParts ? new Date(
  `${demoTime.includes("T") ? demoTime.split("T")[0] : new Intl.DateTimeFormat("sv-SE", { timeZone: "America/New_York" }).format(Date.now())}T${demoParts[1]}:${demoParts[2]}:${demoParts[3] ?? "00"}Z`
) : null;
const bostonWallTime = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
const demoEpoch = demoInitial === null ? null : (() => {
  const target = demoInitial.getTime();
  let epoch = target;
  for (let index = 0; index < 2; index++) {
    const local = Date.parse(bostonWallTime.format(epoch).replace(" ", "T") + "Z");
    epoch += target - local;
  }
  return epoch;
})();
const planningNow = () => demoEpoch === null ? Date.now() : demoEpoch + pageElapsed();

function updateNightClock() {
  setText("nightClock", nightTime.format(planningNow()).replace(",", ""));
}
if (demoTime !== null) {
  byId("demoTime").hidden = false;
  setText("demoTime", demoParts ? "Demo time" : "Invalid demo time");
  setText("nearbyHeading", "Live departures near the restaurant (real time)");
}
updateNightClock();

function duration(minutes) {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return hours ? `${hours} h${rest ? ` ${rest} min` : ""}` : `${rest} min`;
}

function remaining(ms) {
  const totalSeconds = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (!minutes) return `${seconds} sec`;
  return seconds ? `${duration(minutes)} ${seconds} sec` : duration(minutes);
}

function showTimingTile(id, note, iso, clock, describe) {
  const tile = byId(`${id}Tile`);
  const node = byId(id);
  const live = byId(`${id}Live`);
  const detail = byId(`${id}Detail`);
  detail.textContent = "";
  tile.classList.remove("soon", "urgent");
  live.textContent = "";
  node.textContent = note;
  const target = Date.parse(iso);
  if (!Number.isFinite(target)) return null;
  const update = () => {
    const ms = target - clock();
    tile.classList.toggle("urgent", ms < 600000);
    tile.classList.toggle("soon", ms >= 600000 && ms < 1800000);
    const text = describe(ms, duration(Math.max(0, Math.ceil(ms / 60000))));
    const [value, caption = ""] = text.split(" · ");
    if (node.textContent !== value) node.textContent = value;
    if (detail.textContent !== caption) detail.textContent = caption;
    if (live.textContent !== text) live.textContent = text;
  };
  update();
  return update;
}

function showClosing(place, clock) {
  const time = place.closes_at_text || formatTime(place.open_until_iso);
  const note = time === "Open 24 hours" ? time : Number.isFinite(place.closes_in_min)
    ? duration(place.closes_in_min) : "Closing time unavailable";
  closingTick = showTimingTile("closing", note, place.open_until_iso, clock,
    (ms, left) => ms <= 0 ? `Closed at ${time}` : `${left} · ${time}`);
}

function showLastTrain(info, clock, note = "Checking schedules…") {
  const iso = info?.latest_leave_iso;
  const time = formatTime(iso);
  const ride = rideLegs(info?.last_option)[0];
  lastTrainTick = showTimingTile("lastTrain", note, iso, clock,
    (ms, left) => ms <= 0 ? `No ${ride ? vehicleName(ride) : "ride"} home still fits` : `${left} · Leave by ${time}`);
}

function mapsLink(to, from) {
  const mapsUrl = httpUrl(to.maps_url);
  if (!from && mapsUrl) return mapsUrl;
  const hasPin = hasCoordinates(to);
  const address = [to.name, to.address].filter(Boolean).join(", ");
  const destination = hasPin ? coordinates(to).join(",") :
    /,\s*MA\b/i.test(to.address ?? "") ? address : `${address}, Boston, MA`;
  if (!hasPin && !mapsUrl) {
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(destination)}`;
  }
  const params = new URLSearchParams({ api: "1", destination, travelmode: "transit" });
  if (hasCoordinates(from)) params.set("origin", coordinates(from).join(","));
  return `https://www.google.com/maps/dir/?${params}`;
}

function tripParams(from, to) {
  return { from_lat: from.lat, from_lng: from.lng, to_lat: to.lat, to_lng: to.lng };
}

const lineColor = (leg) => typeof leg.color === "string" && /^#[0-9a-f]{6}$/i.test(leg.color) ? leg.color : "#224b3b";

function walkTimeLabel(leg) {
  return needsWalkRoute(leg) || leg.estimated
    ? `about ${leg.duration_min} min${walkJobs.get(walkRouteKey(leg))?.done ? " (estimate)" : ", finding the route…"}`
    : `${leg.duration_min} min`;
}

const routeGeometry = (geometry) => Array.isArray(geometry) && geometry.length >= 2 &&
  geometry.every((point) => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite));
const needsWalkRoute = (leg) => leg?.mode === "walk" &&
  (leg.estimated === true || leg.geometry_kind !== "route" || !routeGeometry(leg.geometry));
const includesEstimatedWalk = (itinerary) => itinerary?.legs.some(needsWalkRoute);
const walkRouteKey = (leg) => leg?.from && leg?.to && hasCoordinates(leg.from) && hasCoordinates(leg.to)
  ? [...coordinates(leg.from), ...coordinates(leg.to)].join(",") : undefined;
const walkJobs = new Map();
const walkDelays = [2000, 4000, 8000, 15000, 15000, 15000];
let refreshSelectedHomeWalk;

function markApproximateLeg(leg, layer, seen, bounds) {
  for (const point of [leg.from, leg.to]) {
    if (!hasCoordinates(point)) continue;
    const key = coordinates(point).join(",");
    if (seen.has(key)) continue;
    seen.add(key);
    const label = `Approximate walk endpoint: ${point.name}`;
    marker({ ...point, name: label }, "approximate-stop", "≈")
      .setZIndexOffset(1600).bindTooltip(iconText(label)).addTo(layer);
    bounds?.extend(coordinates(point));
  }
}

const rideLabel = (leg) => {
  const line = typeof leg.line === "string" ? leg.line : "";
  return leg.vehicle === "bus" ? (line ? `Bus ${line}` : "Bus") : line || "Train";
};
const vehicleName = (leg) => leg.vehicle === "bus" ? "bus" : "train";
const rideLegs = (itinerary) => itinerary?.legs.filter((leg) => leg.mode === "ride") ?? [];

function rideChip(leg, parent) {
  const chip = element("span", "", parent);
  chip.className = "line-chip";
  chip.style.setProperty("--line-color", lineColor(leg));
  chip.append(makeIcon(vehicleName(leg)), " ", rideLabel(leg));
  return chip;
}

function updateHomeVehicle(itinerary, walking = isWalkingPlan()) {
  const ride = rideLegs(itinerary)[0];
  const vehicle = ride ? vehicleName(ride) : walking ? "walk" : "train";
  byId("homeVehicleIcon").replaceChildren(makeIcon(vehicle));
  byId("homeStepIcon").replaceChildren(makeIcon(vehicle));
  setText("lastRideTitle", vehicle === "bus" ? "Last bus home" : "Last T home");
}

function showTimelineLegs(parent, itinerary, section) {
  const list = element("ol", "", parent);
  list.className = "timeline-legs";
  let cursor = itinerary.summary.depart_iso;
  for (const leg of itinerary.legs) {
    const walking = leg.mode === "walk";
    const depart = leg.depart_iso || cursor;
    const arrive = leg.arrive_iso || (Number.isFinite(Date.parse(depart)) && Number.isFinite(leg.duration_min) ?
      new Date(Date.parse(depart) + leg.duration_min * 60000).toISOString() : null);
    cursor = arrive;
    if (walking && !leg.duration_min) continue;
    const row = element("li", "", list);
    row.append(makeIcon(walking ? "walk" : vehicleName(leg)));
    const body = element("div", "", row);
    element("div", walking ? `Walk ${section === "HOME" ? "back " : ""}to ${leg.to.name}`
      : `${section === "GO" ? "Board " : ""}${rideLabel(leg)}${leg.direction_name ? ` → ${leg.direction_name}` : ""}`, body);
    const time = [formatTime(depart), formatTime(arrive)].filter(Boolean).join(" → ");
    const rideMinutes = Math.round((Date.parse(arrive) - Date.parse(depart)) / 60000);
    element("small", walking ? [time, walkTimeLabel(leg)].filter(Boolean).join(" · ") :
      [section === "GO" ? formatTime(depart) : time, `${leg.from.name} → ${leg.to.name}`,
        Number.isFinite(rideMinutes) ? `${rideMinutes} min` : ""].filter(Boolean).join(" · "), body);
  }
}

function showSteps(parent, itinerary) {
  const steps = element("ol", "", parent);
  steps.className = "steps";
  let previousRide;
  for (const leg of itinerary.legs) {
    const step = element("li", "", steps);
    if (leg.mode === "walk") {
      step.append(makeIcon("walk"), " ");
      const time = walkTimeLabel(leg);
      step.append(`Walk from ${leg.from.name} to ${leg.to.name} · ${time}`);
      if (Number.isFinite(leg.distance_m)) {
        const distance = leg.distance_m < 1000 ? `${leg.distance_m} m` : `${(leg.distance_m / 1000).toFixed(2)} km`;
        step.append(` · ${distance}`);
      }
    } else if (leg.mode === "ride") {
      if (previousRide) {
        const wait = Math.round((Date.parse(leg.depart_iso) - Date.parse(previousRide.arrive_iso)) / 60000);
        element("div", `Transfer at ${leg.from.name}${wait >= 0 ? ` · ${wait} min between rides` : ""}`, step).className = "hint";
      }
      rideChip(leg, step);
      if (typeof leg.direction_name === "string" && leg.direction_name) step.append(` toward ${leg.direction_name}`);
      const stops = Number.isInteger(leg.stops) && leg.stops >= 0 ? ` · ${leg.stops} ${leg.stops === 1 ? "stop" : "stops"}` : "";
      element("div", `Board at ${leg.from.name} at ${formatTime(leg.depart_iso)}${stops}`, step);
      element("div", `Exit at ${leg.to.name} at ${formatTime(leg.arrive_iso)}`, step);
      previousRide = leg;
    }
  }
}

function drawItinerary(itinerary) {
  routeLayer.clearLayers();
  const bounds = L.latLngBounds([]);
  const markers = new Map();
  const approximate = new Set();
  for (const leg of itinerary.legs) {
    const walking = leg.mode === "walk";
    if (needsWalkRoute(leg)) {
      markApproximateLeg(leg, routeLayer, approximate, bounds);
      continue;
    }
    if (!routeGeometry(leg.geometry)) continue;
    const path = L.polyline(leg.geometry, {
      color: walking ? "#66616e" : lineColor(leg), weight: walking ? 4 : 5,
      dashArray: walking ? "7 7" : null,
      className: walking ? "walk-route" : leg.vehicle === "bus" ? "bus-route" : "",
      lineCap: "round"
    }).addTo(routeLayer);
    if (leg.returnWalk) path.bindTooltip("Walk home");
    bounds.extend(path.getBounds());
    if (!walking) {
      for (const [action, stop] of [["Board", leg.from], ["Exit", leg.to]]) {
        if (!hasCoordinates(stop)) continue;
        const key = coordinates(stop).join(",");
        if (!markers.has(key)) markers.set(key, { stop, labels: [], leg });
        markers.get(key).labels.push(`${action} ${rideLabel(leg)} at ${stop.name}`);
      }
    }
  }
  for (const { stop, labels, leg } of markers.values()) {
    const label = labels.join(" · ");
    const popup = Object.assign(document.createElement("span"), { textContent: label });
    marker({ ...stop, name: label }, "route-stop", makeIcon(vehicleName(leg)))
      .bindPopup(popup)
      .addTo(routeLayer);
    bounds.extend(coordinates(stop));
  }
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 16, animate: !reducedMotion });
}

const alertEffects = {
  SHUTTLE: "Shuttle buses",
  SUSPENSION: "Suspension",
  DETOUR: "Detour",
  STATION_CLOSURE: "Station closed",
  STOP_CLOSURE: "Stop closed",
  SERVICE_CHANGE: "Service change",
  SCHEDULE_CHANGE: "Schedule change",
  DELAY: "Delay"
};
const alertDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", month: "short", day: "numeric"
});

function alertUntil(iso) {
  const time = formatTime(iso);
  if (!time) return "";
  const date = alertDate.format(Date.parse(iso));
  return `until ${date}, ${time.replace(/:00(?= )/, "")}`;
}

function noServiceMessage(message, alerts) {
  const included = alerts?.find(({ header }) => header && message.includes(header));
  const until = alertUntil(included?.active_until_iso);
  return until ? message.replace(included.header, (header) => `${header} (${until})`) : message;
}

function showServiceAlerts(parent, itineraryAlerts, rootAlerts, message = "") {
  const alerts = [];
  const ids = new Set();
  for (const alert of [...(itineraryAlerts ?? []), ...(rootAlerts ?? [])]) {
    if (!alert.header || ids.has(alert.id)) continue;
    ids.add(alert.id);
    const header = Array.from(alert.header).slice(0, 300).join("");
    if (!message.includes(header)) alerts.push({ ...alert, header });
  }
  if (!alerts.length) return;

  const note = element("details", "", parent);
  note.className = "banner warning service-alert";
  element("summary", alerts.length === 1 ? "Service alert" : `Service alerts (${alerts.length})`, note);

  const showList = (target, entries) => {
    const list = element("ul", "", target);
    for (const { effect, header, active_until_iso } of entries) {
      const item = element("li", "", list);
      const label = Object.hasOwn(alertEffects, effect) ? alertEffects[effect] : "";
      if (label) element("span", label, item).className = "alert-effect";
      element("p", header, item);
      const until = alertUntil(active_until_iso);
      if (until) element("small", until, item);
    }
  };

  showList(note, alerts.slice(0, 3));
  if (alerts.length > 3) {
    const more = element("details", "", note);
    element("summary", `Show more alerts (${alerts.length - 3})`, more);
    showList(more, alerts.slice(3));
  }
}

let timingRun;
let timingClock = planningNow;
let timingSnapshot;
let timingElapsed;
let timingVersion = 0;
let timingAsOf;
let timingPending;
let timingFailures = 0;
let timingRetryAfter = 0;
let timingRequestFailed = false;
let prefetchedUntil = null;
let coverageUntil = null;
let kitchenClosed = false;
let lastTimingRequest = -Infinity;
let lastTimingTick = Date.now();
let routeChoices;
let homeWork;

function timeOfFirstRide(itinerary) {
  return Date.parse(itinerary?.legs.find((leg) => leg.mode === "ride")?.depart_iso);
}

function leaveBy(itinerary) {
  const scheduled = Date.parse(itinerary.summary.leave_by_iso ?? itinerary.summary.depart_iso);
  const board = timeOfFirstRide(itinerary);
  if (!Number.isFinite(board)) return scheduled;
  let walk = 0;
  for (const leg of itinerary.legs) {
    if (leg.mode === "ride") break;
    if (leg.mode === "walk") walk += leg.duration_min * 60000;
  }
  return Number.isFinite(scheduled) ? Math.min(scheduled, board - walk) : board - walk;
}

function sameDeparture(left, right) {
  if (!left) return false;
  const rides = (itinerary) => itinerary.legs.filter((leg) => leg.mode === "ride");
  const prior = rides(left);
  const next = rides(right);
  if (!prior.length || !next.length) {
    return !prior.length && !next.length && left.summary.depart_iso === right.summary.depart_iso;
  }
  return prior.length === next.length && prior.every((leg, index) =>
    leg.route_id === next[index].route_id &&
    (leg.from.stop_id ?? leg.from.name) === (next[index].from.stop_id ?? next[index].from.name) &&
    (leg.to.stop_id ?? leg.to.name) === (next[index].to.stop_id ?? next[index].to.name) &&
    Date.parse(leg.depart_iso) === Date.parse(next[index].depart_iso));
}

function catchable(itinerary, now) {
  return !Number.isFinite(timeOfFirstRide(itinerary)) || leaveBy(itinerary) > now;
}

function recordPlanTime(iso) {
  timingAsOf = Number.isFinite(Date.parse(iso)) ? Date.parse(iso) : timingSnapshot;
  const serverTime = Date.parse(iso);
  const elapsed = timingElapsed;
  timingClock = demoEpoch === null && Number.isFinite(serverTime) ? () => serverTime + elapsed() : planningNow;
  if (directionPlace) showClosing(directionPlace, timingClock);
  setText("timingAsOf", `Times as of ${formatTime(new Date(timingAsOf).toISOString())}`);
}

function resetTiming() {
  timingVersion++;
  byId("refreshTimes").removeAttribute("aria-disabled");
  timingRun = timingPending = routeChoices = homeWork = undefined;
  timingFailures = 0;
  timingRetryAfter = 0;
  timingRequestFailed = false;
  prefetchedUntil = coverageUntil = null;
  kitchenClosed = false;
  setText("kitchenStatus", "");
  markPlanWaiting(false);
  lastTimingRequest = -Infinity;
  timingAsOf = undefined;
  setText("timingProgress", "");
  setText("timingNotice", "");
  setText("timingAsOf", "");
}

function abortTimingRequests() {
  for (const key of ["availability", "route", "home", "walk-go", "walk-return"]) {
    directionRequests.get(key)?.abort();
    directionRequests.delete(key);
  }
}

function requestTimingRefresh(reason, force = false) {
  if (!hasCoordinates(directionPlace) || !hasCoordinates(locationPoint)) return;
  if (document.hidden || byId("planView").hidden || byId("selectedDetails").hidden || communitySubmitPending()) {
    timingPending = reason;
    return;
  }
  if (timingRun && !force && reason !== "stay") {
    if ((reason === "visible" || reason === "wake") && Date.now() - lastTimingRequest > 1000) {
      timingPending = reason;
    }
    return timingRun;
  }
  if (timingRun && reason === "stay") force = true;
  if (!force && Date.now() < timingRetryAfter) {
    timingPending = reason;
    return;
  }
  timingPending = undefined;
  abortTimingRequests();
  const place = directionPlace;
  const point = locationPoint;
  timingSnapshot = planningNow();
  timingElapsed = elapsedClock(Date.now(), performance.now());
  const quiet = reason === "walk-recovery";
  const version = ++timingVersion;
  homeWork = undefined;
  timingRequestFailed = false;
  timingClock = planningNow;
  lastTimingRequest = Date.now();
  if (!quiet) {
    setText("timingProgress", "Updating times…");
    for (const tab of byId("tripTabs").children) tab.setAttribute("aria-disabled", "true");
  }
  if (reason === "expired") markPlanWaiting(true);
  if (reason === "closing") {
    byId("directions").classList.add("plan-waiting");
    setText("kitchenStatus", "Checking whether the kitchen is still open…");
  }
  const run = (async () => {
    if (!await refreshKitchen(place, version) || version !== timingVersion) return;
    await showTrip(place, point, version, reason);
    while (version === timingVersion && homeWork) {
      const work = homeWork;
      await work;
      if (homeWork === work) break;
    }
  })();
  timingRun = run;
  run.finally(() => {
    if (timingRun !== run) return;
    timingRun = undefined;
    setText("timingProgress", "");
    byId("refreshTimes").removeAttribute("aria-disabled");
    if (!byId("directions").classList.contains("plan-waiting")) {
      for (const tab of byId("tripTabs").children) tab.removeAttribute("aria-disabled");
    }
    if (timingRequestFailed && !quiet) {
      timingFailures++;
      timingRetryAfter = Date.now() + (timingFailures <= 6 ? 10000 : 30000);
      if (routeChoices) markPlanWaiting(true);
    } else if (!quiet) {
      timingFailures = 0;
      timingRetryAfter = 0;
    }
  });
  return run;
}

async function refreshKitchen(place, version) {
  const controller = new AbortController();
  directionRequests.set("availability", controller);
  let now = timingSnapshot;
  let current;
  try {
    const response = await get("/api/places", withDemoTime({
      q: place.name, menus: "all", browse: 1
    }, timingSnapshot), 8000, controller);
    current = response.places.find((candidate) => candidate.id === place.id);
    const reported = Date.parse(response.now_iso);
    if (Number.isFinite(reported)) now = reported;
  } catch (error) {
    if (error.name === "AbortError") return false;
  } finally {
    if (directionRequests.get("availability") === controller) directionRequests.delete("availability");
  }
  if (version !== timingVersion || directionPlace !== place) return false;

  const priorClose = place.open_until_iso;
  if (current) {
    for (const key of ["open_now", "open_until_iso", "closes_at_text", "closes_in_min", "hours_unverified", "opens_at_iso", "opens_at_text", "opens_in_min"]) {
      if (Object.hasOwn(current, key)) place[key] = current[key];
    }
  }
  const deadline = Date.parse(place.open_until_iso);
  const lastKnownDeadline = Number.isFinite(deadline) ? deadline : Date.parse(priorClose);
  kitchenClosed = place.open_now === false ||
    (place.open_now !== true || Number.isFinite(deadline)) && Number.isFinite(lastKnownDeadline) && lastKnownDeadline <= now;
  if (!kitchenClosed) {
    setText("kitchenStatus", !current ? "Couldn’t confirm the restaurant’s hours. Check before leaving."
      : place.open_now == null ? "Kitchen hours are unconfirmed. Check before leaving." : "");
    return true;
  }
  recordPlanTime(new Date(now).toISOString());
  const closing = formatTime(place.open_until_iso ?? priorClose);
  if (closing) setText("closing", `Closed at ${closing}`);
  else setText("closing", "Kitchen closed");
  closingTick = null;
  markPlanWaiting(false);
  byId("directions").classList.add("plan-waiting");
  for (const tab of byId("tripTabs").children) tab.setAttribute("aria-disabled", "true");
  setText("kitchenStatus", `The kitchen is closed. This plan is no longer available.${place.opens_at_text ? ` Opens at ${place.opens_at_text}.` : ""}`);
  updateEatTime();
  showStayWarning("");
  tripTick = null;
  return false;
}

function updateKitchenArrival(arrival) {
  const closing = Date.parse(directionPlace?.open_until_iso);
  if (Number.isFinite(closing) && Date.parse(arrival) >= closing) {
    setText("kitchenStatus", `This route arrives after the kitchen closes at ${formatTime(directionPlace.open_until_iso)}.`);
    return false;
  }
  if (byId("kitchenStatus").textContent.startsWith("This route arrives")) setText("kitchenStatus", "");
  return true;
}

function markPlanWaiting(waiting) {
  byId("directions").classList.toggle("plan-waiting", waiting);
  const notice = waiting ? "Looking for the next train…" : "";
  if (byId("timingNotice").textContent !== notice) setText("timingNotice", notice);
  if (waiting) for (const tab of byId("tripTabs").children) tab.setAttribute("aria-disabled", "true");
}

function prunePassedRoutes() {
  if (!routeChoices || kitchenClosed) return;
  const { itineraries, clock, onSelect } = routeChoices;
  const remainingRoutes = itineraries.filter((itinerary) => catchable(itinerary, clock()));
  if (remainingRoutes.length === itineraries.length) return;
  if (!remainingRoutes.length) {
    markPlanWaiting(true);
    if (!timingRun && !timingRetryAfter) return "expired";
    return;
  }
  const sameRoute = remainingRoutes.includes(selectedTrip);
  showTripOptions(remainingRoutes, clock, onSelect, sameRoute ? selectedTrip : undefined, sameRoute, !sameRoute);
  if (!sameRoute) return "route-change";
}

function tickTiming() {
  const wall = Date.now();
  const jumped = Math.abs(wall - lastTimingTick) > 90000;
  lastTimingTick = wall;
  if (document.hidden || byId("planView").hidden || byId("selectedDetails").hidden) return;
  const change = prunePassedRoutes();
  if (change || jumped) requestTimingRefresh(change || "wake");
  if (!timingRun && !kitchenClosed && Date.parse(directionPlace?.open_until_iso) <= timingClock()) {
    requestTimingRefresh("closing");
  }
  if (!timingRun && timingRetryAfter && wall >= timingRetryAfter && !kitchenClosed) {
    requestTimingRefresh("retry");
  }
  if (timingPending && !timingRun) requestTimingRefresh(timingPending);
  const firstRoute = routeChoices?.itineraries.find((itinerary) => !walkOnly(itinerary));
  const firstTrain = timeOfFirstRide(firstRoute);
  const firstDeparture = Number.isFinite(firstTrain) ? firstTrain - timingClock() : Infinity;
  const horizon = routeChoices?.itineraries
    .filter((route) => Number.isFinite(timeOfFirstRide(route)) && catchable(route, timingClock()))
    .reduce((latest, route) => Math.max(latest, leaveBy(route)), -Infinity);
  const validUntil = Math.min(horizon ?? -Infinity, coverageUntil ?? Infinity);
  if (!timingRun && !kitchenClosed && validUntil > timingClock() && validUntil - timingClock() < 180000 &&
      validUntil !== prefetchedUntil && (!timingRetryAfter || wall >= timingRetryAfter)) {
    if (requestTimingRefresh("prefetch")) prefetchedUntil = validUntil;
  }
  if (!timingRun && !kitchenClosed && wall - lastTimingRequest >= 60000 &&
      (firstDeparture >= 0 && firstDeparture <= 600000 ||
       isWalkingPlan() && timingAsOf !== undefined)) {
    requestTimingRefresh("soon");
  }
}

function showTripOptions(itineraries, clock, onSelect, preferred, preserveHome = false, deferHome = false) {
  routeChoices = itineraries.length ? { itineraries, clock, onSelect } : undefined;
  const focusedTab = byId("tripTabs").contains(document.activeElement);
  const tabs = byId("tripTabs");
  const options = byId("tripOptions");
  const stepsOpen = options.querySelector(":scope > :not([hidden]) details")?.open;
  tabs.replaceChildren();
  options.replaceChildren();
  tabs.hidden = !itineraries.length;
  tripTick = null;
  if (!itineraries.length) {
    routeLayer.clearLayers();
    document.dispatchEvent(new Event("night-plan-changed"));
    if (focusedTab) byId("refreshTimes").focus({ preventScroll: true });
    return;
  }
  const panels = [];
  const timers = [];
  const firstRide = itineraries.find((itinerary) => !walkOnly(itinerary));
  let activeIndex;
  const select = (index, userChoice = false) => {
    if (activeIndex === index) return;
    activeIndex = index;
    [...tabs.children].forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    tripTick = timers[index];
    tripTick();
    drawItinerary(itineraries[index]);
    if (!preserveHome || itineraries[index] !== preferred) onSelect(itineraries[index], deferHome && activeIndex === 0);
    document.dispatchEvent(new Event("night-plan-changed"));
    if (userChoice) requestTimingRefresh("focus", true);
  };
  for (const [index, itinerary] of itineraries.entries()) {
    const { summary } = itinerary;
    const panel = element("div", "", options);
    panel.id = `trip-option-${index}`;
    const node = element("p", "", panel);
    node.className = "countdown";
    const live = element("span", "", panel);
    live.className = "sr-only";
    live.setAttribute("role", "status");
    const timedRide = Number.isFinite(timeOfFirstRide(itinerary));
    const deadline = leaveBy(itinerary);
    timers.push(() => {
      const ms = deadline - clock();
      const seconds = Math.max(0, Math.ceil(ms / 1000));
      const text = !timedRide ? "Leave now" : ms <= 0 ? "Departure passed" :
        ms <= 60000 ? `Leave in ${seconds} s` : `Leave at ${formatTime(new Date(deadline).toISOString())} · in ${remaining(ms)}`;
      if (node.textContent !== text) node.textContent = text;
      const announce = timedRide && ms <= 60000 && ms > 0 ? "Leave in less than a minute" : text.split(" · ")[0];
      if (live.textContent !== announce) live.textContent = announce;
      node.classList.toggle("soon", timedRide && ms > 15000 && ms <= 60000);
      node.classList.toggle("urgent", timedRide && ms > 0 && ms <= 15000);
    });
    const estimated = includesEstimatedWalk(itinerary);
    const tripSummary = element("p", `${estimated ? "About " : ""}${summary.duration_min} min total${estimated ? " (estimate)" : ""} · Arrive ${formatTime(summary.arrive_iso)}`, panel);
    const rides = rideLegs(itinerary);
    if (!timedRide && itinerary.long_walk_at_night) {
      element("p", "Long walk at night", panel).className = "hint";
    }
    if (!timedRide && itinerary.long_walk_at_night && firstRide) {
      const board = rideLegs(firstRide)[0];
      const fare = transitFare(firstRide);
      const card = element("button", "", panel);
      card.type = "button";
      card.className = "ride-callout";
      element("strong", "Prefer to ride?", card);
      card.append(` Next ${rideLabel(board)} at ${formatTime(board.depart_iso)}, arrive ${formatTime(firstRide.summary.arrive_iso)}` +
        (Number.isFinite(fare) ? `, ${money.format(fare)} each way` : ""));
      card.addEventListener("click", () => {
        const index = itineraries.indexOf(firstRide);
        if (tabs.children[index]?.getAttribute("aria-disabled") === "true") return;
        select(index, true);
        tabs.children[index]?.focus();
      });
    }
    const details = element("details", "", panel);
    details.open = Boolean(stepsOpen && itinerary === preferred);
    element("summary", "Route steps", details);
    showSteps(details, itinerary);
    details.insertBefore(tripSummary, details.querySelector(".steps"));
    node.classList.add("trip-deadline");
    showTimelineLegs(panel, itinerary, "GO");
    panel.append(details);
    panels.push(panel);
    const leave = formatTime(new Date(deadline).toISOString());
    const fare = transitFare(itinerary);
    const trips = tripFares.trips ?? fareTrips();
    const fareLabel = Number.isFinite(fare) ? ` · ${money.format(fare * trips)} ${trips === 2 ? "round trip" : "one way"}` : "";
    const buses = rides.filter((leg) => leg.vehicle === "bus");
    const mode = !timedRide ? "Walk" : buses.length ? buses.map(rideLabel).join(" → ") : "T";
    const label = timedRide ? `${mode} · ${formatTime(rides[0].depart_iso)}` : mode;
    const tab = element("button", label, tabs);
    element("small", `${summary.duration_min} min${timedRide ? fareLabel : " · free"}`, tab);
    if (timedRide) tab.title = `Leave at ${leave}`;
    tab.type = "button";
    tab.id = `trip-tab-${index}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.tabIndex = 0;
    tab.addEventListener("click", () => {
      if (tab.getAttribute("aria-disabled") !== "true") select(index, true);
    });
    tab.addEventListener("keydown", (event) => {
      if (tab.getAttribute("aria-disabled") === "true") return;
      const last = itineraries.length - 1;
      const next = { ArrowLeft: index ? index - 1 : last, ArrowRight: index === last ? 0 : index + 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(next, true);
      tabs.children[next].focus();
    });
  }
  select(preferred ? itineraries.indexOf(preferred) : 0);
  deferHome = false;
  if (focusedTab) {
    (tabs.querySelector('[aria-selected="true"]') ?? byId("refreshTimes")).focus({ preventScroll: true });
  }
}

function cancelDirections() {
  for (const controller of directionRequests.values()) controller.abort();
  directionRequests.clear();
  resetTiming();
  selectedHomeTrip = null;
  homeModeOverride = undefined;
  tripFares = {};
  walkLegCache.clear();
  for (const job of walkJobs.values()) {
    job.controller?.abort();
    clearTimeout(job.timer);
  }
  walkJobs.clear();
  refreshSelectedHomeWalk = undefined;
  byId("walkEstimateNote").hidden = true;
}

async function loadDirection(panel, path, params, loading, failure, render, { quiet = false, optional = false, silentError = false } = {}) {
  directionRequests.get(panel)?.abort();
  const controller = new AbortController();
  directionRequests.set(panel, controller);
  const spinner = byId(`${panel}Loading`);
  const retry = byId(`${panel}Retry`);
  const status = `${panel}Status`;
  if (!quiet) retry.hidden = true;
  spinner.hidden = quiet;
  spinner.lastElementChild.textContent = "0 s";
  if (loading && !quiet) setText(status, loading);
  const start = performance.now();
  const timer = quiet ? null : setInterval(() => {
    spinner.lastElementChild.textContent = `${Math.floor((performance.now() - start) / 1000)} s`;
  }, 1000);
  controller.signal.addEventListener("abort", () => clearInterval(timer), { once: true });
  try {
    const data = await get(path, params, 25000, controller);
    if (directionRequests.get(panel) !== controller) return;
    const walkingFallback = panel === "route" && data.ok === true && data.upstream_error === true &&
      data.itineraries?.length > 0 && data.itineraries.every(({ legs }) =>
        legs.length > 0 && legs.every(({ mode }) => mode === "walk"));
    if (data.ok === false || (data.upstream_error === true && !walkingFallback)) {
      throw new Error(data.message || failure);
    }
    await render(data, walkingFallback);
    if (directionRequests.get(panel) !== controller) return;
    retry.hidden = optional || !(panel === "route" && path === "/api/trip" && !routeChoices || data.status === "unavailable" ||
      data.stops?.some((stop) => stop.departures_status === "unavailable"));
  } catch (error) {
    if (directionRequests.get(panel) !== controller) return;
    const message = error.name === "TimeoutError" ? "This request timed out after 25 seconds." : error.message || failure;
    if (optional) {
      if (!silentError) setText("rideNote", message);
    } else if (!quiet) {
      if (panel === "route" || panel === "home") timingRequestFailed = true;
      setText(status, message);
      retry.hidden = false;
    }
  } finally {
    clearInterval(timer);
    if (directionRequests.get(panel) === controller) {
      spinner.hidden = true;
      directionRequests.delete(panel);
    }
  }
  return true;
}

let selectedTrip;
let selectedHomeTrip;
let homeModeOverride;
let tripFares = {};
const walkOnly = (itinerary) => itinerary?.legs.length > 0 && !rideLegs(itinerary).length;
const planningLate = () => selectedPickPlace?.late ?? byId("lateMode").checked;
const transitFare = (itinerary) => itinerary && (itinerary.fare ?? itinerary.fare_each ?? tripFares.each);
const directionParams = (from, to) => ({
  ...tripParams(from, to), trips: fareTrips(), ...(planningLate() ? { late: 1 } : {})
});
let outboundAlerts = [];
let returnAlerts = [];

function updateTimelineAlerts() {
  const region = byId("timelineAlerts");
  region.replaceChildren();
  showServiceAlerts(region, outboundAlerts, returnAlerts, [
    byId("routeStatus").textContent, byId("homeStatus").textContent
  ].join(" "));
}

function updateEatTime() {
  const stay = stayMinutes();
  if (kitchenClosed) {
    setText("eatTitle", "");
    setText("eatTime", "Kitchen closed · No meal planned");
    return null;
  }
  const arrival = Date.parse(selectedArrival);
  if (Number.isFinite(arrival) && !updateKitchenArrival(selectedArrival)) {
    setText("eatTitle", "");
    setText("eatTime", "Arrives after closing · No meal planned");
    return null;
  }
  const departure = Number.isFinite(arrival) ? new Date(arrival + stay * 60000).toISOString() : null;
  setText("eatTitle", `${stay} minutes to enjoy your food`);
  setText("eatTime", departure ? `${formatTime(selectedArrival)} → ${formatTime(departure)} · leave by ${formatTime(departure)}` : "");
  return departure;
}

function isWalkingPlan() {
  return selectedTrip ? walkOnly(selectedTrip) : activeWalk;
}

function walkingItinerary(leg, depart, arrive) {
  const depart_iso = new Date(depart).toISOString();
  return { mode: "walk", kind: "walk", fare: 0, legs: [leg], summary: { depart_iso, leave_by_iso: depart_iso,
    arrive_iso: new Date(arrive).toISOString(), walk_min: leg.duration_min, duration_min: leg.duration_min } };
}

function routeMaps(to, from, walkingMode = isWalkingPlan()) {
  const url = mapsLink(to, from);
  if (!walkingMode) return url;
  const walking = new URL(url);
  if (walking.searchParams.has("travelmode")) walking.searchParams.set("travelmode", "walking");
  return walking.href;
}

function refreshMapLinks(place, point) {
  byId("routeLinks").replaceChildren();
  byId("homeLinks").replaceChildren();
  link(byId("routeLinks"), "Open in Maps", routeMaps(place, point));
  if (point && hasCoordinates(place)) link(byId("homeLinks"), "Maps home", routeMaps(point, place, selectedHomeTrip ? walkOnly(selectedHomeTrip) : (homeModeOverride ?? isWalkingPlan())));
}

function updateModeFare(place, point) {
  refreshMapLinks(place, point);
  budgetLabel();
  if (currentDish) renderDishPrice(currentDish);
}

function walkLegFromRoute(route, from, to) {
  return {
    mode: "walk", from, to, distance_m: route.distance_m,
    duration_min: Math.ceil(route.duration_s / 60), geometry: route.estimated === true ? [] : route.geometry,
    estimated: route.estimated === true || route.geometry_kind !== "route",
    geometry_kind: route.estimated === true ? "none" : route.geometry_kind, message: route.message
  };
}

function localWalkEstimate(from, to) {
  if (!hasCoordinates(from) || !hasCoordinates(to)) return null;
  // Leaflet's Earth CRS computes the great-circle (haversine) distance in meters.
  const geometry = [coordinates(from), coordinates(to)];
  const distance_m = Math.round(map.distance(...geometry) * 1.25);
  return walkLegFromRoute({
    distance_m, duration_s: distance_m / (4.8 / 3.6), geometry: [], estimated: true, geometry_kind: "none",
    message: "Walking time is an estimate right now."
  }, from, to);
}

const walkLegCache = new Map();

async function loadWalkLeg(key, from, to, retryEstimate = false) {
  const cached = walkLegCache.get(key);
  if (cached && (!cached.estimated || !retryEstimate)) return cached;
  if (!hasCoordinates(from) || !hasCoordinates(to)) return null;
  const controller = new AbortController();
  directionRequests.get(key)?.abort();
  directionRequests.set(key, controller);
  let leg;
  try {
    const route = await get("/api/route", tripParams(from, to), 8000, controller);
    if (route.ok && Number.isFinite(route.duration_s) && route.duration_s >= 0 &&
        Number.isFinite(route.distance_m) && route.distance_m >= 0 &&
        (route.estimated === true || route.geometry_kind === "none" || routeGeometry(route.geometry))) {
      leg = walkLegFromRoute(route, from, to);
    }
  } catch (error) {
    if (error.name === "AbortError") return null;
    // A provider or transport failure still permits a local estimate.
  } finally {
    if (directionRequests.get(key) === controller) directionRequests.delete(key);
  }
  if (controller.signal.aborted && controller.signal.reason?.name !== "TimeoutError") return null;
  const local = !leg && localWalkEstimate(from, to);
  if (local) local.localFallback = true;
  leg = leg && (!leg.estimated || cached?.localFallback) ? leg : cached ?? leg ?? local;
  if (leg) walkLegCache.set(key, leg);
  return leg;
}

function showWalkingEstimateNote() {
  const note = byId("walkEstimateNote");
  const legs = [selectedTrip, selectedHomeTrip].flatMap((route) => route?.legs.filter(needsWalkRoute) ?? []);
  const pending = legs.some((leg) => walkRouteKey(leg) && !walkJobs.get(walkRouteKey(leg))?.done);
  const state = !legs.length ? "" : pending ? "pending" : "unavailable";
  if (note.dataset.state === state && note.dataset.walking === String(isWalkingPlan())) return;
  note.dataset.state = state;
  note.dataset.walking = String(isWalkingPlan());
  note.replaceChildren();
  note.hidden = !state;
  if (!state) return;
  note.append(pending ? "Walking time is an estimate while we find the route. " :
    "Walking route unavailable right now. Time is an estimate. ");
  link(note, "Maps", routeMaps(directionPlace, locationPoint));
}

function applyRecoveredWalks(itinerary) {
  if (!itinerary) return false;
  let changed = false;
  itinerary.legs = itinerary.legs.map((leg) => {
    const real = needsWalkRoute(leg) && walkJobs.get(walkRouteKey(leg))?.route;
    if (!real) return leg;
    changed = true;
    return walkLegFromRoute(real, leg.from, leg.to);
  });
  if (!changed) return false;
  const legs = itinerary.legs;
  const first = legs.findIndex((leg) => leg.mode === "ride");
  const last = legs.findLastIndex((leg) => leg.mode === "ride");
  const duration = (steps) => steps.reduce((sum, leg) => sum + (leg.mode === "walk" ? leg.duration_min : 0), 0);
  const summary = itinerary.summary;
  const walk = duration(legs);
  const depart = first < 0 ? Date.parse(summary.depart_iso) : Date.parse(legs[first].depart_iso) - duration(legs.slice(0, first)) * 60000;
  const arrive = last < 0 ? depart + walk * 60000 : Date.parse(legs[last].arrive_iso) + duration(legs.slice(last + 1)) * 60000;
  Object.assign(summary, { depart_iso: new Date(depart).toISOString(), leave_by_iso: new Date(depart).toISOString(),
    arrive_iso: new Date(arrive).toISOString(), duration_min: Math.ceil((arrive - depart) / 60000), walk_min: walk });
  return true;
}

function repaintRecoveredWalks() {
  const previousArrival = selectedArrival;
  const changed = applyRecoveredWalks(selectedTrip);
  if (changed) {
    selectedArrival = selectedTrip.summary.arrive_iso;
  }
  if (routeChoices && selectedTrip) {
    const { itineraries, clock, onSelect } = routeChoices;
    showTripOptions(itineraries, clock, onSelect, selectedTrip, true);
  } else if (selectedTrip && changed) {
    drawItinerary(selectedTrip);
    const panel = byId("tripOptions");
    const details = panel.querySelector("details");
    const leg = selectedTrip.legs[0];
    panel.querySelector("p").textContent = `Leave now · Walk ${walkTimeLabel(leg)} · Arrive ${formatTime(selectedArrival)}`;
    details.querySelector("ol").remove();
    showSteps(details, selectedTrip);
  }
  if (changed && previousArrival !== selectedArrival) {
    homeWork = showHome(directionPlace, locationPoint, "walk-recovery");
  } else {
    refreshSelectedHomeWalk?.();
  }
  showWalkingEstimateNote();
}

function recoverFocusedWalks() {
  if (kitchenClosed || !directionPlace) return;
  const legs = [selectedTrip, selectedHomeTrip].flatMap((route) => route?.legs.filter(needsWalkRoute) ?? []);
  for (const leg of legs) {
    const key = walkRouteKey(leg);
    if (!key) continue;
    let job = walkJobs.get(key);
    if (!job) {
      job = { attempts: 0, next: 0 };
      walkJobs.set(key, job);
    }
    if (job.controller || job.done || job.next > Date.now()) continue;
    const controller = new AbortController();
    job.controller = controller;
    get("/api/route", tripParams(leg.from, leg.to), 8000, controller).then((route) => {
      if (route.ok && route.geometry_kind === "route" && route.estimated !== true && routeGeometry(route.geometry) &&
          Number.isFinite(route.duration_s) && route.duration_s >= 0 && Number.isFinite(route.distance_m)) job.route = route;
    }).catch(() => {}).finally(() => {
      if (walkJobs.get(key) !== job) return;
      job.controller = null;
      const delay = walkDelays[job.attempts++];
      job.done = Boolean(job.route) || delay === undefined;
      job.next = Date.now() + (delay ?? 0);
      if (!job.done) job.timer = setTimeout(recoverFocusedWalks, delay);
      if (job.done && [selectedTrip, selectedHomeTrip].some((route) => route?.legs.some(
        (item) => needsWalkRoute(item) && walkRouteKey(item) === key))) repaintRecoveredWalks();
    });
  }
}

document.addEventListener("night-plan-changed", recoverFocusedWalks);

async function showWalkTrip(place, point, version, reason) {
  const quiet = reason === "walk-recovery";
  const retryEstimate = quiet || reason === "manual" || reason === "retry";
  const previousGo = walkLegCache.get("walk-go");
  const previousHome = walkLegCache.get("walk-return");
  if (!previousGo && !quiet) setText("routeStatus", "Planning your walk…");
  const [leg, returnLeg] = await Promise.all([
    loadWalkLeg("walk-go", point, place, retryEstimate),
    loadWalkLeg("walk-return", place, point, retryEstimate)
  ]);
  if (version !== timingVersion) return;
  if (!leg || !returnLeg) {
    setText("routeStatus", "Walking directions couldn’t load.");
    byId("routeRetry").hidden = false;
    return;
  }
  if (quiet && leg === previousGo && returnLeg === previousHome) return;

  recordPlanTime();
  markPlanWaiting(false);
  const depart = timingClock();
  selectedArrival = new Date(depart + leg.duration_min * 60000).toISOString();
  selectedTrip = walkingItinerary(leg, depart, Date.parse(selectedArrival));
  if (!byId("nightLoading").hidden) updateLoading(2);
  const detailsOpen = byId("tripOptions").querySelector("details")?.open;
  showTripOptions([]);
  byId("routeRetry").hidden = true;
  setText("routeStatus", "");
  const itinerary = selectedTrip;
  const panel = byId("tripOptions");
  element("p", `Leave now · Walk ${walkTimeLabel(leg)} · Arrive about ${formatTime(selectedArrival)}`, panel);
  showTimelineLegs(panel, itinerary, "GO");
  const details = element("details", "", panel);
  element("summary", "Walking route", details);
  details.open = Boolean(detailsOpen);
  showSteps(details, itinerary);
  showWalkingEstimateNote();
  setText("longWalkNote", (selectedPickPlace?.late ?? byId("lateMode").checked) && leg.duration_min > 15 ? "Long walk at night" : "");
  homeWork = showHome(place, point, reason, returnLeg);
}

function configureWalkAlternative(place) {
  const trip = currentDish ?? place;
  const savings = trip.walk_saves ?? trip.fare;
  const offered = !activeWalk && trip.walk_alternative === true &&
    Number.isFinite(trip.walk_min) && Number.isFinite(savings);
  offerFix("walkInstead", offered ? `Walk instead · ${trip.walk_min} min · saves ${money.format(savings)}` : null, () => {
    activeWalk = true;
    selectedTrip = selectedHomeTrip = null;
    updateModeFare(place, locationPoint);
    requestTimingRefresh("manual", true);
    byId("nightHeading").focus({ preventScroll: true });
  });
}

async function showTrip(place, point, version, reason) {
  const quiet = reason === "walk-recovery";
  let rendered = false;
  const received = await loadDirection("route", "/api/trip", withDemoTime({ ...directionParams(point, place), focus: selectedTrip?.focusIndex ?? 0 }, timingSnapshot),
    timingAsOf === undefined ? "Planning your trip…" : null, "Trip planning couldn’t load.", async (data) => {
      recordPlanTime(data.now_iso);
      if (!Array.isArray(data.itineraries)) throw new Error("Trip response had no route information.");
      tripFares = { each: data.fare_each_transit, trips: data.fare_trips };
      const offered = data.itineraries.map((itinerary, focusIndex) => ({ ...itinerary, focusIndex }));
      for (const itinerary of offered) applyRecoveredWalks(itinerary);
      const walkIndex = offered.findIndex(walkOnly);
      const cachedWalk = walkLegCache.get("walk-go");
      if ((walkIndex < 0 && activeWalk) || (walkIndex >= 0 && includesEstimatedWalk(offered[walkIndex]) &&
          (quiet || cachedWalk?.estimated === false))) {
        const leg = await loadWalkLeg("walk-go", point, place, quiet || reason === "manual" || reason === "retry");
        if (version !== timingVersion) return;
        if (leg) {
          const depart = timingClock();
          const walk = walkingItinerary(leg, depart, depart + leg.duration_min * 60000);
          if (walkIndex < 0) offered.unshift(walk);
          else if (!leg.estimated || includesEstimatedWalk(offered[walkIndex])) offered[walkIndex] = walk;
        }
      }
      for (const itinerary of offered.filter(walkOnly)) {
        itinerary.long_walk_at_night ||= planningLate() && itinerary.summary.walk_min > 15;
      }
      rendered = true;
      const valid = Date.parse(data.valid_until_iso);
      coverageUntil = Number.isFinite(valid) ? valid : null;
      const available = offered.filter((itinerary) => catchable(itinerary, timingClock()));
      const walking = available.find(walkOnly);
      const itineraries = (walking ? [walking, ...available.filter((item) => item !== walking)] : available).slice(0, 5);
      if (!itineraries.length && offered.length) {
        timingRequestFailed = true;
        markPlanWaiting(true);
        return;
      }
      markPlanWaiting(false);
      const message = data.message ?? "";
      outboundAlerts = data.alerts ?? [];
      setText("routeStatus", itineraries.length ? message :
        noServiceMessage(message || "No trains or buses are scheduled for this trip.", data.alerts));
      if (itineraries.length) {
        const preferred = itineraries.find((itinerary) => sameDeparture(selectedTrip, itinerary)) ??
          (selectedTrip ? itineraries.find((itinerary) => walkOnly(itinerary) === walkOnly(selectedTrip)) :
            activeWalk ? walking : available[0]);
        showTripOptions(itineraries, timingClock, (itinerary, deferHome) => {
          directionRequests.get("home")?.abort();
          selectedTrip = itinerary;
          if (!byId("nightLoading").hidden) updateLoading(2);
          selectedHomeTrip = null;
          selectedArrival = itinerary.summary.arrive_iso;
          refreshMapLinks(place, point);
          budgetLabel();
          if (currentDish) renderDishPrice(currentDish);
          offerFix("walkInstead");
          if (!isWalkingPlan() && !walking) configureWalkAlternative(place);
          setText("longWalkNote", "");
          showWalkingEstimateNote();
          outboundAlerts = [...(itinerary.alerts ?? []), ...(data.alerts ?? [])];
          if (deferHome) {
            updateEatTime();
            byId("homeOption").replaceChildren();
            byId("homeModes").hidden = true;
            setText("homeStatus", "");
            setText("rideNote", "");
            showStayWarning("");
          } else {
            homeWork = showHome(place, point, reason);
          }
        }, preferred);
      } else {
        showTripOptions([]);
        selectedTrip = null;
        selectedHomeTrip = null;
        selectedArrival = undefined;
        showWalkingEstimateNote();
        refreshMapLinks(place, point);
        budgetLabel();
        if (currentDish) renderDishPrice(currentDish);
        updateEatTime();
        byId("homeOption").replaceChildren();
        byId("homeModes").hidden = true;
        setText("homeStatus", "");
        showStayWarning("");
        showLastTrain(null, timingClock, "No outbound trip");
        const next = formatTime(data.next_departure_iso);
        setText("timingNotice", next ? `No train fits any more. Next departure: ${next}.` : "No train fits any more.");
        updateTimelineAlerts();
      }
    }, { quiet });
  if (!rendered && activeWalk && version === timingVersion && !routeChoices) {
    await showWalkTrip(place, point, version, reason);
    return;
  }
  if (received && version === timingVersion && !byId("routeRetry").hidden && selectedArrival === undefined) {
    setText("homeStatus", "Refresh to check the return trip.");
  }
}

function showStayWarning(message, minutes) {
  byId("homeWarning").hidden = !message;
  setText("stayWarningTitle", minutes ? "Timing warning" : "Last train & closing");
  setText("stayWarning", message);
  offerFix("stayFix", minutes ? `Stay ${minutes} min instead` : null, () => {
    setStay(minutes);
    byId("eatTime").focus();
  });
}

function renderReturnWalk(place, point, leg, plannedLeave, detailsOpen, version) {
  const option = byId("homeOption");
  option.replaceChildren();
  setText("homeStatus", "");
  const closing = Date.parse(place.open_until_iso);
  const depart = Math.min(Date.parse(plannedLeave), Number.isFinite(closing) ? closing : Infinity);
  const arrive = Number.isFinite(depart) ? new Date(depart + leg.duration_min * 60000).toISOString() : null;
  selectedHomeTrip = arrive ? walkingItinerary(leg, depart, Date.parse(arrive)) : null;
  updateHomeVehicle(undefined, true);
  updateModeFare(place, point);
  showWalkingEstimateNote();
  document.dispatchEvent(new Event("night-plan-changed"));
  if (selectedHomeTrip) showTimelineLegs(option, selectedHomeTrip, "HOME");
  element("p", `Walk home · ${walkTimeLabel(leg)}${arrive ? ` · Arrive about ${formatTime(arrive)}` : ""}`, option);
  const details = element("details", "", option);
  element("summary", "Return walking route", details);
  details.open = Boolean(detailsOpen);
  showSteps(details, { legs: [leg] });
  const showReturn = element("button", "Show return route on map", details);
  showReturn.type = "button";
  showReturn.addEventListener("click", () => { setMapOpen(true); drawItinerary(selectedHomeTrip); });
  if (selectedTrip) drawItinerary(selectedTrip);
  showWalkingEstimateNote();
  offerFix("walkReturnRetry", leg.localFallback ? "Retry walk home" : null, async () => {
    if (directionRequests.has("walk-return")) return;
    const button = byId("walkReturnRetry");
    button.disabled = true;
    try {
      const replacement = await loadWalkLeg("walk-return", place, point, true);
      if (replacement && directionPlace === place && locationPoint === point &&
          version === homeRenderVersion && walkOnly(selectedHomeTrip) && !kitchenClosed) {
        renderReturnWalk(place, point, replacement, plannedLeave, details.open, version);
        if (button.hidden && document.activeElement === document.body) {
          option.querySelector("summary")?.focus({ preventScroll: true });
        }
      }
    } finally {
      button.disabled = false;
    }
  });
}

async function showHome(place, point, reason, knownReturn) {
  const quiet = reason === "walk-recovery";
  const version = ++homeRenderVersion;
  refreshSelectedHomeWalk = undefined;
  selectedHomeTrip = null;
  updateHomeVehicle();
  offerFix("walkReturnRetry");
  const option = byId("homeOption");
  const detailsOpen = option.querySelector("details")?.open;
  if (!quiet) option.replaceChildren();
  returnAlerts = [];
  byId("homeModes").hidden = true;
  const plannedLeave = updateEatTime();
  if (selectedArrival && !plannedLeave) {
    showWalkingEstimateNote();
    setText("homeStatus", "No meal or return trip planned for this route.");
    setText("rideNote", "");
    showLastTrain(null, timingClock, "No return trip planned");
    return;
  }
  if (!quiet) setText("rideNote", "");
  const stay = stayMinutes();
  const params = withDemoTime({ ...directionParams(place, point), stay_min: stay }, timingSnapshot);
  if (selectedArrival) params.arrive_iso = selectedArrival;
  if (place.open_until_iso) params.closes_iso = place.open_until_iso;
  const closing = Date.parse(place.open_until_iso);
  const timeLeft = (deadline) => Math.floor((deadline - Date.parse(selectedArrival)) / 60000);
  const shorterStay = (deadline) => [90, 60, 45, 30].find((minutes) => minutes < stay && minutes <= timeLeft(deadline));
  const closesEarly = Date.parse(plannedLeave) > closing;
  const closingWarning = closesEarly ? `A ${stay}-minute stay would go past closing at ${formatTime(place.open_until_iso)}.` : "";
  if (!quiet || isWalkingPlan()) showStayWarning(closingWarning, closesEarly ? shorterStay(closing) : undefined);

  let walking = homeModeOverride ?? isWalkingPlan();
  let returnLeg, rideData;
  const modes = byId("homeModes");
  modes.hidden = true;
  modes.replaceChildren();
  const current = () => directionPlace === place && locationPoint === point && homeRenderVersion === version && !kitchenClosed;
  const renderModes = (focus = false) => {
    modes.replaceChildren();
    const canRide = Boolean(rideData?.last_option);
    modes.hidden = !returnLeg || walking && !canRide;
    if (modes.hidden) return;
    for (const walk of canRide ? [walking, !walking] : [true]) {
      const fare = transitFare(rideData?.last_option);
      const text = walk ? `Walk home · ${walkTimeLabel(returnLeg)} · free` :
        `Ride home · ${rideData.last_option.summary.duration_min} min${Number.isFinite(fare) ? ` · ${money.format(fare)}` : ""}`;
      const button = element("button", text, modes);
      button.type = "button";
      button.setAttribute("aria-pressed", String(walk === walking));
      if (focus && walk === walking) button.focus({ preventScroll: true });
      button.addEventListener("click", () => {
        if (!current() || walk === walking) return;
        homeModeOverride = walking = walk;
        renderSelectedHome();
        renderModes(true);
      });
    }
  };

  const renderRide = (data) => {
    offerFix("walkReturnRetry");
    option.replaceChildren();
    const last = data.last_option;
    const message = data.message ?? "";
    const clock = timingClock;
    returnAlerts = [...(last?.alerts ?? []), ...(data.alerts ?? [])];
    const homeEstimated = includesEstimatedWalk(last);
    setText("homeStatus", last ? message : "");
    if (!last) {
      selectedHomeTrip = null;
      updateHomeVehicle(undefined, false);
      updateModeFare(place, point);
      if (selectedTrip) drawItinerary(selectedTrip);
      setText("homeStatus", noServiceMessage(message || "No return itinerary was provided.", data.alerts));
      showLastTrain(null, clock, "No ride home");
      updateTimelineAlerts();
      return;
    }
    selectedHomeTrip = { ...last,
      summary: { ...last.summary, depart_iso: data.latest_leave_iso } };
    updateHomeVehicle(last);
    showWalkingEstimateNote();
    document.dispatchEvent(new Event("night-plan-changed"));
    updateModeFare(place, point);
    showLastTrain(data, clock);
    const service = rideLegs(last)[0]?.vehicle === "bus" ? "bus" : "T";
    const fitsStay = Date.parse(plannedLeave) <= Date.parse(data.latest_leave_iso);
    const trainWarning = data.warning || (data.stay_ok === false || !fitsStay ? `Staying ${stay} minutes would miss the last ${service} home.` : "");
    const lastLeave = Date.parse(data.latest_leave_iso);
    const deadline = Number.isFinite(closing) ? Math.min(closing, lastLeave) : lastLeave;
    showStayWarning([closingWarning, trainWarning].filter(Boolean).join(" "),
      closesEarly || !fitsStay ? shorterStay(deadline) : undefined);
    const depart = formatTime(data.last_train_depart_iso);
    showTimelineLegs(option, selectedHomeTrip, "HOME");
    element("p", `${fitsStay ? "Leave by" : `Last ${service} option · leave by`} ${formatTime(data.latest_leave_iso)} · Home ${formatTime(last.summary.arrive_iso)}`, option);
    if (data.last_train_from && depart) element("p", `Last ${service} home: ${depart} · leave restaurant by ${formatTime(data.latest_leave_iso)}`, option).className = "hint";
    if (homeEstimated) element("p", `About ${last.summary.duration_min} min total (estimate)`, option).className = "hint";
    const details = element("details", "", option);
    element("summary", "Return steps", details);
    details.open = Boolean(detailsOpen);
    showSteps(details, last);
    const showReturn = element("button", "Show return route on map", details);
    showReturn.type = "button";
    showReturn.addEventListener("click", () => { setMapOpen(true); drawItinerary(last); });
    if (selectedTrip) drawItinerary(selectedTrip);
    updateTimelineAlerts();
  };

  function renderSelectedHome() {
    if (walking) {
      showStayWarning(closingWarning, closesEarly ? shorterStay(closing) : undefined);
      showLastTrain(null, timingClock, "Walk home");
      updateHomeVehicle(undefined, true);
      if (returnLeg) renderReturnWalk(place, point, returnLeg, plannedLeave, detailsOpen, version);
      const depart = formatTime(rideData?.last_train_depart_iso);
      const leave = formatTime(rideData?.latest_leave_iso);
      const vehicle = rideLegs(rideData?.last_option)[0]?.vehicle === "bus" ? "bus" : "T";
      if (depart) setText("rideNote", `Or ride: last ${vehicle} ${depart}${leave ? ` · leave by ${leave}` : ""}`);
      returnAlerts = [...(rideData?.last_option?.alerts ?? []), ...(rideData?.alerts ?? [])];
      updateTimelineAlerts();
    } else {
      setText("rideNote", "");
      if (rideData) renderRide(rideData);
    }
  }

  if (walking) {
    showLastTrain(null, timingClock, "Walk home");
    updateHomeVehicle(undefined, true);
    if (!quiet && !knownReturn && !walkLegCache.has("walk-return")) setText("homeStatus", "Planning your walk home…");
    byId("homeRetry").hidden = true;
  }
  const walkingWork = Promise.resolve(knownReturn ?? loadWalkLeg("walk-return", place, point,
    quiet || reason === "manual" || reason === "retry")).then((leg) => {
    if (!current()) return;
    returnLeg = leg;
    if (walking) renderSelectedHome();
    renderModes();
  });
  const rideWork = loadDirection("home", "/api/home", params,
    "Checking the last ride…", "The last ride home couldn’t load.", (data) => {
      if (!current()) return;
      const last = data.last_option;
      const leave = Date.parse(data.latest_leave_iso ?? last?.summary.depart_iso);
      rideData = last && leave >= Date.parse(selectedArrival) ? {
        ...data, latest_leave_iso: data.latest_leave_iso ?? last.summary.depart_iso,
        last_option: { ...last, fare: last.fare ?? last.fare_each ?? data.fare_each_transit }
      } : { ...data, last_option: null };
      if (applyRecoveredWalks(rideData.last_option)) rideData.latest_leave_iso = rideData.last_option.summary.depart_iso;
      renderSelectedHome();
      renderModes();
    }, { quiet: quiet || walking, optional: walking, silentError: quiet });
  refreshSelectedHomeWalk = () => {
    if (!current()) return;
    if (walking && returnLeg) {
      const real = walkJobs.get(walkRouteKey(returnLeg))?.route;
      if (needsWalkRoute(returnLeg) && real) {
        returnLeg = walkLegFromRoute(real, returnLeg.from, returnLeg.to);
        walkLegCache.set("walk-return", returnLeg);
      }
    } else if (rideData?.last_option && applyRecoveredWalks(rideData.last_option)) {
      rideData.latest_leave_iso = rideData.last_option.summary.depart_iso;
    }
    renderSelectedHome();
    renderModes();
  };
  await Promise.all([walkingWork, rideWork]);

  if (directionPlace === place && locationPoint === point && !directionRequests.has("home") && !byId("homeRetry").hidden) {
    if (!quiet) showLastTrain(null, timingClock, "Schedules unavailable");
    updateTimelineAlerts();
  }
}

function departureLabel(departure) {
  const line = departure.line || departure.route_id?.replace("-", " ") || "";
  if (!departure.direction_name) return line || "Train";
  return line ? `${line} toward ${departure.direction_name}` : `Train toward ${departure.direction_name}`;
}

function renderNearby({ stops, message, now_iso }) {
  const list = byId("stops");
  list.replaceChildren();
  const notice = message || (stops.length ? "" : "No nearby T stops were found.");
  if (byId("nearbyStatus").textContent !== notice) setText("nearbyStatus", notice);
  const responseTime = Date.parse(now_iso);
  const fallbackStart = Number.isFinite(responseTime) ? responseTime : Date.now();
  const upcoming = [];
  for (const stop of stops) {
    const item = element("li", "", list);
    element("strong", stop.name, item);
    const distance = Number.isFinite(stop.distance_km) ? `${stop.distance_km.toFixed(2)} km` : "";
    const walk = Number.isFinite(stop.walk_min) ? `${stop.walk_min} min walk` : "";
    element("div", [stop.lines.join(", "), distance, walk].filter(Boolean).join(" · "), item);
    if (stop.departures_status === "unavailable") {
      element("div", "Departures unavailable for this stop", item);
      continue;
    }
    if (stop.departures_status === "none") {
      element("div", "No upcoming departures", item);
      continue;
    }

    const departures = (stop.departures ?? []).map((departure) => {
      const timestamp = Date.parse(departure.departure_iso);
      const time = Number.isFinite(timestamp) ? timestamp : fallbackStart + departure.minutes * 60000;
      return { label: departureLabel(departure), time };
    }).filter(({ time }) => Number.isFinite(time) && time > Date.now());
    const status = element("div", "", item);
    for (const departure of departures) departure.node = element("div", "", item);
    upcoming.push({ name: stop.name, status, departures });
  }
  nearbyTick = () => {
    const now = Date.now();
    const announcements = [];
    for (const stop of upcoming) {
      const summary = [];
      stop.departures = stop.departures.filter(({ time, node }) => {
        if (time > now) return true;
        node.remove();
        return false;
      });
      for (const { time, label, node } of stop.departures) {
        const ms = time - now;
        const due = ms < 60000;
        const text = `${label} · ${due ? "Due" : remaining(ms)}`;
        if (node.textContent !== text) node.textContent = text;
        summary.push(`${label} · ${due ? "Due" : duration(Math.floor(ms / 60000))}`);
      }
      const empty = summary.length ? "" : "No upcoming departures";
      if (stop.status.textContent !== empty) stop.status.textContent = empty;
      announcements.push(`${stop.name}: ${empty || summary.join(", ")}`);
    }
    const live = byId("nearbyLive");
    const summary = announcements.join(". ");
    if (live.textContent !== summary) live.textContent = summary;
  };
  nearbyTick();
}

function showNearby(point, background = false) {
  if (document.hidden || !hasCoordinates(point) || !byId("nearbyDisclosure").open || directionRequests.has("nearby")) return;
  nearbyRequestedAt = performance.now();
  return loadDirection("nearby", "/api/nearby", { lat: point.lat, lng: point.lng },
    background ? null : "Loading nearby stops and departures…", "Nearby stops couldn’t load.", renderNearby);
}

function showDirections(place) {
  cancelDirections();
  directionPlace = place;
  byId("directions").hidden = false;
  setText("longWalkNote", "");
  setText("rideNote", "");
  offerFix("walkReturnRetry");
  offerFix("walkInstead");
  selectedArrival = undefined;
  selectedTrip = null;
  outboundAlerts = returnAlerts = [];
  routeLayer.clearLayers();
  tripTick = nearbyTick = null;
  updateEatTime();
  setText("nearbyLive", "");
  setText("nearbyStatus", "");
  setText("homeStatus", "");
  for (const panel of ["route", "home", "nearby"]) {
    byId(`${panel}Loading`).hidden = true;
    byId(`${panel}Retry`).hidden = true;
  }
  for (const id of ["routeLinks", "homeLinks", "tripTabs", "tripOptions", "homeOption", "homeModes", "stops", "timelineAlerts"]) byId(id).replaceChildren();
  byId("tripTabs").hidden = true;
  byId("homeModes").hidden = true;
  byId("homeWarning").hidden = true;
  byId("homeWarning").querySelector("details").open = false;
  refreshMapLinks(place, locationPoint);
  if (!hasCoordinates(place)) setText("nearbyStatus", "Map coordinates are unavailable for this place.");
  byId("refreshTimes").hidden = !locationPoint || !hasCoordinates(place);
  if (locationPoint && hasCoordinates(place)) {
    configureWalkAlternative(place);
    if (activeWalk) {
      showLastTrain(null, Date.now, "Walk home");
      return requestTimingRefresh("initial", true);
    }
    const verified = currentDish?.transit;
    showLastTrain(verified?.mode === "train" ? verified : null, planningNow,
      verified?.mode === "walk" ? "Walk home" : "Checking schedules…");
    return requestTimingRefresh("initial", true);
  }
  showLastTrain(null, Date.now, locationPoint ? "Route unavailable" : "Choose a start");
  setText("routeStatus", locationPoint ? "Map coordinates are unavailable for this place." :
    "Change plan to choose a start for your trip.");
  setText("homeStatus", "A route needs a start and restaurant location.");
}

byId("refreshTimes").addEventListener("click", () => requestTimingRefresh("manual", true));
byId("routeRetry").addEventListener("click", () => requestTimingRefresh("retry", true));
byId("homeRetry").addEventListener("click", () => requestTimingRefresh("retry", true));
byId("nearbyRetry").addEventListener("click", () => showNearby(directionPlace));
byId("nearbyDisclosure").addEventListener("toggle", (event) => {
  if (event.currentTarget.open) showNearby(directionPlace);
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    const request = directionRequests.get("nearby");
    request?.abort();
    if (request) {
      directionRequests.delete("nearby");
      byId("nearbyLoading").hidden = true;
    }
  } else {
    lastTimingTick = Date.now();
    requestTimingRefresh(prunePassedRoutes() || "visible");
    if (byId("nearbyDisclosure").open) {
      nearbyTick?.();
      showNearby(directionPlace, Boolean(nearbyTick));
    }
  }
});

setInterval(() => {
  updateNightClock();
  tickTiming();
  closingTick?.();
  lastTrainTick?.();
  tripTick?.();
  if (!document.hidden && byId("nearbyDisclosure").open) {
    nearbyTick?.();
    if (performance.now() - nearbyRequestedAt >= 30000) showNearby(directionPlace, true);
  }
}, 1000);

budget.addEventListener("input", () => {
  budgetLabel();
  if (currentDish) renderDishPrice(currentDish);
  schedulePlaces();
  scheduleLateStrip();
});
form.addEventListener("change", (event) => {
  if (event.target.name === "stay") {
    scheduleLateStrip();
    requestTimingRefresh("stay");
  }
  if (event.target.name === "walk_ok_min") {
    walkOk = Number(event.target.value);
    try {
      localStorage.setItem("midnight-munch-walk-ok", String(walkOk));
    } catch {
      // The chosen limit still applies for this page.
    }
    budgetLabel();
    schedulePlaces();
    scheduleLateStrip();
  }
});
byId("countRideHome").addEventListener("change", () => {
  try {
    localStorage.setItem("midnight-munch-fare-trips", String(fareTrips()));
  } catch {
    // The switch still applies for this page.
  }
  budgetLabel();
  schedulePlaces();
  scheduleLateStrip();
});
byId("search").addEventListener("input", schedulePlaces);
byId("favoritesOnly").addEventListener("change", () => { renderPlaces(); schedulePlaces(); });
for (const id of ["cuisine", "browseAll", "menus"]) byId(id).addEventListener("change", schedulePlaces);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  spin(false);
});
byId("reroll").addEventListener("click", () => spin(true));
byId("retryOptions").addEventListener("click", loadOptions);
byId("retryPlaces").addEventListener("click", schedulePlaces);
map.on("moveend", schedulePlaces);
schedulePlaces();
scheduleLateStrip();
loadOptions();
