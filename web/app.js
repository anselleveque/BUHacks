"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("preferences");
const station = byId("station");
const budget = byId("budget");
const walk = byId("walk");
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
let walkingReturn;
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
  walkOk = options.includes(saved) ? saved : options.includes(config?.default) ? config.default : options.includes(15) ? 15 : options[0];
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
  const includesFare = locationPoint && subwayFare !== undefined && !activeWalk;
  setText("budgetTitle", `Budget $${budget.value} · ${activeWalk ? "dish only on foot" : includesFare ? `dish + ${fareKind(fareTrips())} T fare` : "per dish"}`);
  byId("budgetFareHelp").hidden = !includesFare;
  setText("budgetFareHelp", includesFare
    ? `${money.format(subwayFare)} ${fareTrips() === 2 ? "each way" : "one way"} beyond a ${walkOk} minute walk · ${transferNote()}` : "");
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
    if (options.map && options.map.tile_url) {
      map.attributionControl.removeAttribution(tiles.options.attribution);
      tiles.options.attribution = options.map.provider === "maptiler"
        ? '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> ' + osmAttribution
        : osmAttribution;
      tiles.options.maxZoom = options.map.max_zoom || 19;
      map.attributionControl.addAttribution(options.map.attribution);
      tiles.setUrl(options.map.tile_url);
    }
    budget.min = options.slider.min;
    budget.max = options.slider.max;
    budget.step = options.slider.step;
    budget.value = 12;
    subwayFare = Number.isFinite(options.fare?.subway) ? options.fare.subway : undefined;
    transferFare = options.fare?.transfer ?? 0;
    const defaultFareTrips = options.fare?.trips === 1 ? 1 : 2;
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
    for (const area of options.areas) byId("area").add(new Option(area, area));
    for (const minutes of options.walk_options) {
      walk.add(new Option(minutes === null ? "Anywhere on the T" : `Up to ${minutes} min`, minutes ?? ""));
    }
    setCuisineOptions(options.cuisines);
    schedulePlaces();
    walk.value = "";
    walk.disabled = !locationPoint;
    byId("filters").disabled = false;
    byId("spin").disabled = false;
    setText("optionsStatus", "");
    scheduleLateStrip();
  } catch {
    setCuisineOptions();
    setText("optionsStatus", "Spin preferences couldn’t load. Retry to enable Spin. You can still browse nearby places.");
    byId("retryOptions").hidden = false;
  }
}

async function loadStations() {
  try {
    const data = await get("data/stations.json");
    for (const stop of data.sort((a, b) => a.name.localeCompare(b.name))) {
      stations.set(stop.id, stop);
      station.add(new Option(`${stop.name} (${stop.lines.join(", ")})`, stop.id));
    }
    station.disabled = false;
  } catch {
    byId("locationHelp").textContent = "The station list couldn’t load. Try device location on localhost or HTTPS, or reload to retry.";
  }
}

const map = L.map("map", { zoomAnimation: !reducedMotion, fadeAnimation: !reducedMotion,
  markerZoomAnimation: !reducedMotion, keyboardPanDelta: 80 }).setView([42.351, -71.093], 13);
const mapResize = new ResizeObserver(() => map.invalidateSize({ pan: false, debounceMoveend: true }));
mapResize.observe(byId("map"));
const osmAttribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: osmAttribution
}).addTo(map);
tiles.on("tileerror", () => {
  setText("tileStatus", "Some map background tiles couldn’t load.");
});
tiles.on("loading", () => setText("tileStatus", ""));
const placeMarkers = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const hasCoordinates = (place) => Number.isFinite(place?.lat) && Number.isFinite(place?.lng);
const coordinates = (place) => [place.lat, place.lng];

const iconPaths = {
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 6v6l4 2",
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
  placeholder.replaceWith(makeIcon(placeholder.dataset.icon));
}

function iconText(symbol) {
  const node = document.createElement("span");
  node.textContent = symbol;
  return node;
}

function marker(place, style, symbol) {
  const pin = L.marker(coordinates(place), {
    icon: L.divIcon({ className: `pin ${style}`, html: symbol instanceof HTMLElement ? symbol : iconText(symbol), iconSize: [32, 32], popupAnchor: [0, -14] }),
    title: place.name,
    alt: place.name,
    zIndexOffset: style.includes("selected") ? 1000 : style === "user" ? 500 : 0
  });
  pin.on("add", () => pin.getElement().setAttribute("aria-label", place.name));
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
  element("p", place.hours_text || "Hours unavailable", popup);
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
  const style = [selected && "selected", noMenu && "no-menu", picked ? "picked" : openLate && "late"].filter(Boolean).join(" ");
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
    [place.name, noMenu && "No public menu", picked && "Your pick", openLate && "Open past midnight", stamp.getAttribute("aria-label")].filter(Boolean).join(", ")));
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

function renderPlaces() {
  placeMarkers.clearLayers();
  const list = byId("placeList");
  const activeId = list.contains(document.activeElement) ? document.activeElement.dataset.placeId : null;
  list.replaceChildren();
  const origin = locationPoint || map.getCenter();
  for (const place of nearbyPlaces) {
    if (hasCoordinates(place) && place.id !== pickedPlace?.id) placeMarker(place).addTo(placeMarkers);
    const button = element("button", "", element("li", "", list));
    button.type = "button";
    button.className = "restaurant";
    button.dataset.placeId = place.id;
    button.setAttribute("aria-pressed", String(place.id === pickedPlace?.id));
    button.append(makeIcon("pin"));
    const info = element("span", "", button);
    const name = element("strong", place.name + " ", info);
    name.append(stampIcon(place));
    const miles = hasCoordinates(place) ? map.distance(origin, coordinates(place)) / 1609.344 : null;
    element("small", [place.area, miles === null ? null : `${miles.toFixed(1)} mi`, placeCloses(place)].filter(Boolean).join(" · "), info);
    const pricing = element("span", "", button);
    pricing.className = "place-pricing";
    showPriceTier(place, pricing);
    if (place.has_menu === false) element("small", "No public menu", pricing);
    if (Number.isFinite(place.from_price)) showListPrice(place, pricing);
    addPlaceCommunity(place, info);
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
    cuisine: byId("cuisine").value, menus: byId("menus").value, browse: byId("browseAll").checked ? 1 : 0
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
    const count = nearbyPlaces.length;
    const empty = params.q
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

function placeCloses(place) {
  if (place.closes_at_text === "Open 24 hours") return "Open 24 hours";
  const time = place.closes_at_text || formatTime(place.open_until_iso);
  return time ? `Closes ${time}` : "";
}

function scheduleLateStrip() {
  clearTimeout(lateStripTimer);
  lateStripRequest?.abort();
  lateStripTimer = setTimeout(loadLateStrip, 200);
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
  const request = new AbortController();
  lateStripRequest = request;
  setText("lateStripStatus", "Checking late kitchens…");
  list.replaceChildren();
  try {
    const data = await get("/api/places", withDemoTime({
      lat: point.lat, lng: point.lng, radius_km: 8, late: 1, menus: "with", budget: budget.value, walk_ok_min: walkOk, trips: fareTrips(), min_minutes: stay
    }), 15000, request);
    const verified = data.places.filter((place) => place.late_eligible === true);
    const check = async (place) => {
      if (place.mode === "walk" && Number.isFinite(place.walk_min)) {
        return { place, time: `${place.walk_min} min walk`, minutes: place.walk_min };
      }
      const trip = await get("/api/trip", withDemoTime(tripParams(point, place)), 15000, request);
      const best = trip.itineraries?.[0];
      if (!best) return null;
      const minutes = Math.max(best.summary.duration_min, Math.ceil(
        (Date.parse(best.summary.arrive_iso) - Date.parse(trip.now_iso)) / 60000
      ) || 0);
      return { place, time: `${minutes} min ${best.legs.some((leg) => leg.mode === "ride") ? "T" : "walk"}`, minutes };
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
      if (Number.isFinite(place.from_price)) showListPrice(place, info, point);
      else showPriceTier(place, info);
      element("small", `${placeCloses(place)} · ${time}`, info);
      addPlaceCommunity(place, info);
      row.addEventListener("click", () => planPlace({ ...place, now_iso: data.now_iso }, true));
    }
    if (feasible.length) {
      setText("lateStripStatus", "");
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
  walk.disabled = !point;
  renderOrigin();
  if (point) {
    map.setView(coordinates(point), Math.max(map.getZoom(), 13), { animate: !reducedMotion });
  } else {
    walk.value = "";
  }
  setText("locationStatus", point && !stations.has(point.id) ? `Starting from ${point.name}.` : "");
  schedulePlaces();
  scheduleLateStrip();
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
    setText("communityPrice", "");
    showClosing(place, place.now_iso ? responseClock(place.now_iso) : planningNow);
    showAboutPlace(place);
  }
  byId("heroReport").hidden = !currentDish;
  highlight(place);
  setNightTab("plan");
  byId("planSetup").hidden = true;
  byId("selectedDetails").hidden = false;
  byId("aboutDisclosure").open = byId("nearbyDisclosure").open = false;
  renderSelectedDetails();
  byId("planScroll").scrollTop = 0;
  if (planRoute) showDirections(place);
  byId("nightHeading").focus({ preventScroll: true });
}

function changePlan() {
  pickRequest?.abort();
  activeWalk = false;
  selectedPickPlace = undefined;
  budgetLabel();
  byId("selectedDetails").hidden = true;
  byId("planSetup").hidden = false;
  byId("result").hidden = true;
  setText("resultStatus", "");
  offerFix("resultRetry");
  currentDish = directionPlace = null;
  closingTick = lastTrainTick = tripTick = nearbyTick = null;
  cancelDirections();
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
  budgetLabel();
  rememberDish(dish);
  setText("dish", dish.name);
  const note = typeof dish.price_note === "string" ? dish.price_note.trim() : "";
  byId("price").title = /^sizes:/i.test(note) ? note : "";
  renderDishPrice(dish);
  const badges = byId("dishBadges");
  badges.replaceChildren();
  if (dish.community === true) element("span", communityDishLabel(dish), badges).className = "community-chip";
  if (place.open_late === true) element("span", "Open late", badges);
  else if (place.open_now === true) element("span", "Open now", badges);
  if (place.cuisine) element("span", place.cuisine, badges);
  if (dish.price_status === "verify") element("span", "Price may vary", badges);
  for (const tag of dish.tags.filter((tag) => dietOptions.includes(tag)).slice(0, 2)) element("span", tag, badges);
  showAboutPlace(place, dish);
  if (note) element("p", note, byId("dishDetails")).className = "hint";
  showClosing(place, clock);
  byId("result").hidden = false;
  selectPlace(place);
}

function offerFix(id, label, action) {
  const button = byId(id);
  button.hidden = !label;
  button.textContent = label ?? "";
  button.onclick = action ?? null;
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
  directionPlace = null;
  closingTick = lastTrainTick = tripTick = nearbyTick = null;
  byId("directions").hidden = true;
  selectPlace(place, false);
  showLastTrain(null, Date.now, "Checking schedules…");
  spin(false, selectedPickPlace);
}

async function spin(reroll, specific = reroll ? selectedPickPlace : undefined, initialProgress) {
  if (pickRequest || (!specific && byId("spin").disabled)) return;
  const start = locationPoint;
  const feedback = byId("selectedDetails").hidden ? "status" : "resultStatus";
  offerFix("planFix");
  offerFix("resultRetry");
  if (!start && !specific) {
    if (feedback === "resultStatus") changePlan();
    setText("status", "Choose where you are first.");
    (station.disabled ? byId("locate") : station).focus();
    return;
  }
  if (!reroll) shown.clear();
  if (!specific) selectedPickPlace = undefined;
  const fields = new FormData(form);
  const params = { budget: budget.value, open_now: fields.get("open_now") || "0",
    min_minutes: stayMinutes(), walk_ok_min: walkOk, trips: fareTrips() };
  if (start) Object.assign(params, { lat: start.lat, lng: start.lng });
  if (specific?.late ?? fields.has("late")) params.late = "1";
  if (specific) params.place_id = specific.place.id;
  for (const name of ["diet", "craving", "line", "area", "max_walk_min"]) {
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
  const progress = params.late && start ? setTimeout(() => {
    setText(progressTarget, "Checking trains there and home…");
  }, 2200) : null;
  try {
    const { dish, message, relaxed, now_iso, t_timeout } = await get("/api/pick", withDemoTime(params), 30000, request);
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
    if (dish) {
      shown.add(dish.item_id);
      setText("status", "");
      setText("resultStatus", message || (relaxed?.length ? `Adjusted filters: ${relaxed.map((key) => key.replaceAll("_", " ")).join(", ")}.` : ""));
      showDish(dish, clock, start);
    } else {
      setText(feedback, message || "No dishes matched your plan.");
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
      if (specific && !currentDish) showDirections(specific.place);
    }
  } finally {
    clearTimeout(progress);
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
const demoParts = /^(?:\d{4}-\d{2}-\d{2}T)?([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/.exec(demoTime);
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
  `${demoTime.includes("T") ? demoTime.split("T")[0] : new Intl.DateTimeFormat("sv-SE", { timeZone: "America/New_York" }).format(Date.now())}T${demoParts[1]}:${demoParts[2]}:00Z`
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

function countdown(node, live, iso, clock, describe) {
  const target = Date.parse(iso);
  return () => {
    if (!Number.isFinite(target)) return;
    const ms = target - clock();
    const minutes = Math.max(0, Math.ceil(ms / 60000));
    const visible = describe(ms, remaining(Math.max(0, ms)));
    const announced = describe(ms, duration(minutes));
    if (node.textContent !== visible) node.textContent = visible;
    if (live.textContent !== announced) live.textContent = announced;
  };
}

function showTimingTile(id, note, iso, clock, describe) {
  const tile = byId(`${id}Tile`);
  const node = byId(id);
  const live = byId(`${id}Live`);
  tile.classList.remove("soon", "urgent");
  live.textContent = "";
  node.textContent = note;
  const target = Date.parse(iso);
  if (!Number.isFinite(target)) return null;
  const tick = countdown(node, live, iso, clock, (ms) => describe(ms, duration(Math.max(0, Math.ceil(ms / 60000)))));
  const update = () => {
    const ms = target - clock();
    tile.classList.toggle("urgent", ms < 600000);
    tile.classList.toggle("soon", ms >= 600000 && ms < 1800000);
    tick();
  };
  update();
  return update;
}

function showClosing(place, clock) {
  const time = place.closes_at_text || formatTime(place.open_until_iso);
  const note = time === "Open 24 hours" ? time : Number.isFinite(place.closes_in_min)
    ? `${duration(place.closes_in_min)} · ${time}` : "Closing time unavailable";
  closingTick = showTimingTile("closing", note, place.open_until_iso, clock,
    (ms, left) => ms <= 0 ? `Closed at ${time}` : `${left} · ${time}`);
}

function showLastTrain(info, clock, note = "Checking schedules…") {
  const iso = info?.latest_leave_iso;
  const time = formatTime(iso);
  lastTrainTick = showTimingTile("lastTrain", note, iso, clock,
    (ms, left) => ms <= 0 ? "No train home still fits" : `Leave by ${time} · in ${left}`);
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

const lineColor = (leg) => /^#[0-9a-f]{6}$/i.test(leg.color) ? leg.color : "#224b3b";

function showSteps(parent, itinerary) {
  const steps = element("ol", "", parent);
  steps.className = "steps";
  let previousRide;
  for (const leg of itinerary.legs) {
    const step = element("li", "", steps);
    if (leg.mode === "walk") {
      const distance = leg.distance_m < 1000 ? `${leg.distance_m} m` : `${(leg.distance_m / 1000).toFixed(2)} km`;
      step.append(makeIcon("walk"), " ");
      const time = leg.estimated === true ? `about ${leg.duration_min} min (estimated)` : `${leg.duration_min} min`;
      step.append(`Walk from ${leg.from.name} to ${leg.to.name} · ${time} · ${distance}`);
    } else if (leg.mode === "ride") {
      if (previousRide) {
        const wait = Math.round((Date.parse(leg.depart_iso) - Date.parse(previousRide.arrive_iso)) / 60000);
        element("div", `Transfer at ${leg.from.name}${wait >= 0 ? ` · ${wait} min between trains` : ""}`, step).className = "hint";
      }
      const chip = element("span", leg.line, step);
      chip.className = "line-chip";
      chip.style.setProperty("--line-color", lineColor(leg));
      step.append(` toward ${leg.direction_name}`);
      element("div", `Board at ${leg.from.name} at ${formatTime(leg.depart_iso)} · ${leg.stops} ${leg.stops === 1 ? "stop" : "stops"}`, step);
      element("div", `Exit at ${leg.to.name} at ${formatTime(leg.arrive_iso)}`, step);
      previousRide = leg;
    }
  }
}

function drawItinerary(itinerary) {
  routeLayer.clearLayers();
  const bounds = L.latLngBounds([]);
  const markers = new Map();
  for (const leg of itinerary.legs) {
    const walking = leg.mode === "walk";
    const estimated = walking && leg.estimated === true;
    const path = L.polyline(leg.geometry, {
      color: walking ? "#66616e" : lineColor(leg), weight: walking ? 4 : 5,
      dashArray: estimated ? "1 8" : walking ? "7 7" : null,
      lineCap: "round"
    }).addTo(routeLayer);
    if (estimated) path.bindTooltip("Estimated walk · straight-line distance");
    bounds.extend(path.getBounds());
    if (!walking) {
      for (const [action, stop] of [["Board", leg.from], ["Exit", leg.to]]) {
        if (!hasCoordinates(stop)) continue;
        const key = coordinates(stop).join(",");
        if (!markers.has(key)) markers.set(key, { stop, labels: [] });
        markers.get(key).labels.push(`${action} ${leg.line} at ${stop.name}`);
      }
    }
  }
  for (const { stop, labels } of markers.values()) {
    const label = labels.join(" · ");
    const popup = Object.assign(document.createElement("span"), { textContent: label });
    marker({ ...stop, name: label }, "route-stop", "T")
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
  return left?.summary.depart_iso === right.summary.depart_iso &&
    left?.summary.arrive_iso === right.summary.arrive_iso &&
    timeOfFirstRide(left) === timeOfFirstRide(right);
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
  lastTimingRequest = -Infinity;
  timingAsOf = undefined;
  setText("timingProgress", "");
  setText("timingNotice", "");
  setText("timingAsOf", "");
}

function abortTimingRequests() {
  for (const key of ["route", "home", "walk-return"]) {
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
  if (timingRun && reason !== "stay") {
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
  const version = ++timingVersion;
  homeWork = undefined;
  timingClock = planningNow;
  lastTimingRequest = Date.now();
  byId("refreshTimes").setAttribute("aria-disabled", "true");
  setText("timingProgress", "Updating times…");
  for (const tab of byId("tripTabs").children) tab.setAttribute("aria-disabled", "true");
  if (reason === "expired") setText("timingNotice", "No train fits any more. Next options:");
  else if (reason !== "retry") setText("timingNotice", "");
  const run = (async () => {
    if (activeWalk) await showWalkTrip(place, point, version);
    else await showTrip(place, point, version);
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
    for (const tab of byId("tripTabs").children) tab.removeAttribute("aria-disabled");
    if (!byId("routeRetry").hidden || !byId("homeRetry").hidden) {
      timingFailures++;
      timingRetryAfter = Date.now() + Math.min(120000, 15000 * 2 ** Math.min(timingFailures - 1, 3));
    } else {
      timingFailures = 0;
      timingRetryAfter = 0;
    }
  });
  return run;
}

function prunePassedRoutes() {
  if (!routeChoices) return;
  const { itineraries, clock, onSelect } = routeChoices;
  const remainingRoutes = itineraries.filter((itinerary) => catchable(itinerary, clock()));
  if (remainingRoutes.length === itineraries.length) return;
  const selected = selectedTrip;
  if (remainingRoutes.length) {
    // Replace the selected route only if it has actually become unreachable.
    const sameRoute = remainingRoutes.includes(selected);
    showTripOptions(remainingRoutes, clock, onSelect, sameRoute ? selected : undefined, sameRoute, !sameRoute);
    if (!sameRoute) return "route-change";
  } else {
    showTripOptions([]);
    selectedTrip = null;
    selectedArrival = undefined;
    updateEatTime();
    byId("homeOption").replaceChildren();
    showStayWarning("");
    setText("timingNotice", "No train fits any more. Next options:");
    return "expired";
  }
}

function tickTiming() {
  const wall = Date.now();
  const jumped = Math.abs(wall - lastTimingTick) > 90000;
  lastTimingTick = wall;
  if (document.hidden || byId("planView").hidden || byId("selectedDetails").hidden) return;
  const change = prunePassedRoutes();
  if (change || jumped) requestTimingRefresh(change || "wake");
  if (timingPending && !timingRun) requestTimingRefresh(timingPending);
  const firstRoute = routeChoices?.itineraries[0];
  const firstTrain = timeOfFirstRide(firstRoute);
  const firstDeparture = Number.isFinite(firstTrain) ? firstTrain - timingClock() : Infinity;
  if (!timingRun && wall - lastTimingRequest >= 60000 &&
      (firstDeparture >= 0 && firstDeparture <= 600000 ||
       (activeWalk || firstRoute && !Number.isFinite(firstTrain)) && timingAsOf !== undefined)) {
    requestTimingRefresh("soon");
  }
}

function showTripOptions(itineraries, clock, onSelect, preferred, preserveHome = false, deferHome = false) {
  routeChoices = itineraries.length ? { itineraries, clock, onSelect } : undefined;
  const focusedTab = byId("tripTabs").contains(document.activeElement);
  const tabs = byId("tripTabs");
  const options = byId("tripOptions");
  tabs.replaceChildren();
  options.replaceChildren();
  tabs.hidden = itineraries.length < 2;
  tripTick = null;
  if (!itineraries.length) {
    routeLayer.clearLayers();
    if (focusedTab) byId("refreshTimes").focus({ preventScroll: true });
    return;
  }
  const panels = [];
  const timers = [];
  let activeIndex;
  const select = (index) => {
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
      const text = !timedRide ? "Leave now" : ms <= 0 ? "Updating departure…" :
        ms <= 60000 ? `Leave in ${seconds} s` : `Leave at ${formatTime(new Date(deadline).toISOString())} · in ${remaining(ms)}`;
      if (node.textContent !== text) node.textContent = text;
      const announce = timedRide && ms <= 60000 && ms > 0 ? "Leave in less than a minute" : text.split(" · ")[0];
      if (live.textContent !== announce) live.textContent = announce;
      node.classList.toggle("soon", timedRide && ms > 15000 && ms <= 60000);
      node.classList.toggle("urgent", timedRide && ms > 0 && ms <= 15000);
    });
    element("p", `${summary.duration_min} min total · Arrive ${formatTime(summary.arrive_iso)}`, panel);
    const rides = itinerary.legs.filter((leg) => leg.mode === "ride");
    element("p", rides.length ? rides.map((leg) => `${leg.line} → ${leg.direction_name}`).join(" · ")
      : `Walk ${summary.walk_min} min`, panel).className = "hint";
    const details = element("details", "", panel);
    element("summary", "Route steps", details);
    showSteps(details, itinerary);
    panels.push(panel);
    if (itineraries.length === 1) continue;
    const tab = element("button", `Route ${index + 1}`, tabs);
    tab.type = "button";
    tab.id = `trip-tab-${index}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.tabIndex = 0;
    tab.addEventListener("click", () => {
      if (tab.getAttribute("aria-disabled") !== "true") select(index);
    });
    tab.addEventListener("keydown", (event) => {
      if (tab.getAttribute("aria-disabled") === "true") return;
      const last = itineraries.length - 1;
      const next = { ArrowLeft: index ? index - 1 : last, ArrowRight: index === last ? 0 : index + 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(next);
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
}

async function loadDirection(panel, path, params, loading, failure, render) {
  directionRequests.get(panel)?.abort();
  const controller = new AbortController();
  directionRequests.set(panel, controller);
  const spinner = byId(`${panel}Loading`);
  const retry = byId(`${panel}Retry`);
  const status = `${panel}Status`;
  retry.hidden = true;
  spinner.hidden = false;
  spinner.lastElementChild.textContent = "0 s";
  if (loading) setText(status, loading);
  const start = performance.now();
  const timer = setInterval(() => {
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
    render(data, walkingFallback);
    retry.hidden = !(panel === "route" && path === "/api/trip" && !routeChoices || data.status === "unavailable" ||
      data.stops?.some((stop) => stop.departures_status === "unavailable"));
  } catch (error) {
    if (directionRequests.get(panel) !== controller) return;
    setText(status, error.name === "TimeoutError" ? "This request timed out after 25 seconds." : error.message || failure);
    retry.hidden = false;
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
  const arrival = Date.parse(selectedArrival);
  const departure = Number.isFinite(arrival) ? new Date(arrival + stay * 60000).toISOString() : null;
  setText("eatTime", departure ? `${formatTime(selectedArrival)}–${formatTime(departure)} · ${stay} min` : `Plan to stay ${stay} min`);
  return departure;
}

function routeMaps(to, from) {
  const url = mapsLink(to, from);
  if (!activeWalk) return url;
  const walking = new URL(url);
  if (walking.searchParams.has("travelmode")) walking.searchParams.set("travelmode", "walking");
  return walking.href;
}

function walkLegFromRoute(route, from, to) {
  return {
    mode: "walk", from, to, distance_m: route.distance_m,
    duration_min: Math.ceil(route.duration_s / 60), geometry: route.geometry
  };
}

async function showWalkTrip(place, point, version) {
  const routeController = new AbortController();
  directionRequests.get("walk-return")?.abort();
  directionRequests.set("walk-return", routeController);
  walkingReturn = get("/api/route", tripParams(place, point), 25000, routeController).then((route) =>
    route.ok ? walkLegFromRoute(route, place, point) : null, () => null);
  selectedArrival = undefined;
  selectedTrip = null;
  showTripOptions([]);
  updateEatTime();
  setText("homeStatus", "");
  await loadDirection("route", "/api/route", tripParams(point, place),
    "Planning your walk…", "Walking directions couldn’t load.", (route) => {
      const leg = walkLegFromRoute(route, point, place);
      recordPlanTime();
      selectedArrival = new Date(timingClock() + leg.duration_min * 60000).toISOString();
      const itinerary = { legs: [leg] };
      const panel = byId("tripOptions");
      setText("routeStatus", "");
      element("p", `Leave now · Walk ${leg.duration_min} min · Arrive about ${formatTime(selectedArrival)}`, panel);
      const details = element("details", "", panel);
      element("summary", "Walking route", details);
      showSteps(details, itinerary);
      setText("longWalkNote", (selectedPickPlace?.late ?? byId("lateMode").checked) && leg.duration_min > 20 ? "Long walk at night" : "");
      drawItinerary(itinerary);
      homeWork = showHome(place, point);
    });
  if (version === timingVersion && selectedArrival === undefined) {
    setText("homeStatus", "Refresh to check the return trip.");
  }
}

function configureWalkAlternative(place) {
  const trip = currentDish ?? place;
  const savings = trip.walk_saves ?? trip.fare;
  const offered = !activeWalk && trip.walk_alternative === true &&
    Number.isFinite(trip.walk_min) && Number.isFinite(savings);
  offerFix("walkInstead", offered ? `Walk instead · ${trip.walk_min} min · saves ${money.format(savings)}` : null, () => {
    activeWalk = true;
    budgetLabel();
    if (currentDish) renderDishPrice(currentDish);
    showDirections(place);
    byId("nightHeading").focus({ preventScroll: true });
  });
}

async function showTrip(place, point, version) {
  const received = await loadDirection("route", "/api/trip", withDemoTime(tripParams(point, place), timingSnapshot),
    timingAsOf === undefined ? "Planning your trip…" : null, "Trip planning couldn’t load.", (data) => {
      recordPlanTime(data.now_iso);
      const itineraries = (data.itineraries ?? []).filter((itinerary) => catchable(itinerary, timingClock())).slice(0, 3);
      const message = data.message ?? "";
      outboundAlerts = data.alerts ?? [];
      setText("routeStatus", itineraries.length ? message :
        noServiceMessage(message || "No train or bus fits this trip right now.", data.alerts));
      if (itineraries.length) {
        const preferred = itineraries.find((itinerary) => sameDeparture(selectedTrip, itinerary));
        showTripOptions(itineraries, timingClock, (itinerary, deferHome) => {
          directionRequests.get("home")?.abort();
          selectedTrip = itinerary;
          selectedArrival = itinerary.summary.arrive_iso;
          outboundAlerts = [...(itinerary.alerts ?? []), ...(data.alerts ?? [])];
          if (deferHome) {
            updateEatTime();
            byId("homeOption").replaceChildren();
            setText("homeStatus", "");
            setText("rideNote", "");
            showStayWarning("");
          } else {
            homeWork = showHome(place, point);
          }
        }, preferred);
      } else {
        showTripOptions([]);
        selectedTrip = null;
        selectedArrival = undefined;
        updateEatTime();
        byId("homeOption").replaceChildren();
        setText("homeStatus", "");
        showStayWarning("");
        showLastTrain(null, timingClock, "No outbound trip");
        setText("timingNotice", "");
        updateTimelineAlerts();
      }
    });
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

async function showHome(place, point) {
  const version = ++homeRenderVersion;
  const option = byId("homeOption");
  option.replaceChildren();
  returnAlerts = [];
  const plannedLeave = updateEatTime();
  setText("rideNote", "");
  const stay = stayMinutes();
  const params = withDemoTime({ ...tripParams(place, point), stay_min: stay }, timingSnapshot);
  if (selectedArrival) params.arrive_iso = selectedArrival;
  if (place.open_until_iso) params.closes_iso = place.open_until_iso;
  const closing = Date.parse(place.open_until_iso);
  const timeLeft = (deadline) => Math.floor((deadline - Date.parse(selectedArrival)) / 60000);
  const shorterStay = (deadline) => [90, 60, 45, 30].find((minutes) => minutes < stay && minutes <= timeLeft(deadline));
  const closesEarly = Date.parse(plannedLeave) > closing;
  const closingWarning = closesEarly ? `A ${stay}-minute stay would go past closing at ${formatTime(place.open_until_iso)}.` : "";
  showStayWarning(closingWarning, shorterStay(closing));

  if (activeWalk) {
    showLastTrain(null, Date.now, "Walk home");
    setText("homeStatus", "");
    const returnRoute = walkingReturn;
    returnRoute?.then((leg) => {
      if (directionPlace !== place || locationPoint !== point || !activeWalk || walkingReturn !== returnRoute ||
          version !== homeRenderVersion) return;
      if (!leg) {
        element("p", "Return walking route unavailable. Open walking directions in Maps.", option);
        return;
      }
      const arrive = plannedLeave ? new Date(Date.parse(plannedLeave) + leg.duration_min * 60000).toISOString() : null;
      element("p", `Walk home · ${leg.duration_min} min${arrive ? ` · Arrive about ${formatTime(arrive)}` : ""}`, option);
      const details = element("details", "", option);
      element("summary", "Return walking route", details);
      showSteps(details, { legs: [leg] });
    });
    await loadDirection("home", "/api/home", params, null, "Last T times are unavailable.", (data) => {
      const depart = formatTime(data.last_train_depart_iso);
      const leave = formatTime(data.latest_leave_iso);
      setText("rideNote", depart ? `Or ride: last T ${depart}${leave ? ` · leave by ${leave}` : ""}` : "");
      returnAlerts = [...(data.last_option?.alerts ?? []), ...(data.alerts ?? [])];
      setText("homeStatus", "");
      updateTimelineAlerts();
    });
    return;
  }

  const renderWalkingHome = () => {
    if (!plannedLeave || !selectedTrip?.legs.length || selectedTrip.legs.some(({ mode }) => mode !== "walk")) return false;
    const arrive = new Date(Date.parse(plannedLeave) + selectedTrip.summary.walk_min * 60000).toISOString();
    element("p", `Walk home around ${formatTime(plannedLeave)} · Arrive about ${formatTime(arrive)}`, option);
    showLastTrain(null, Date.now, "Walk home");
    return true;
  };

  await loadDirection("home", "/api/home", params,
    "Checking the last train…", "The last train home couldn’t load.", (data) => {
      const last = data.last_option;
      const message = data.message ?? "";
      const clock = timingClock;
      returnAlerts = [...(last?.alerts ?? []), ...(data.alerts ?? [])];
      setText("homeStatus", last ? message : "");
      if (!last) {
        if (!renderWalkingHome()) {
          setText("homeStatus", noServiceMessage(message || "No return itinerary was provided.", data.alerts));
          showLastTrain(null, clock, "No train home");
        }
        updateTimelineAlerts();
        return;
      }
      showLastTrain(data, clock);
      const fitsStay = Date.parse(plannedLeave) <= Date.parse(data.latest_leave_iso);
      const trainWarning = data.warning || (data.stay_ok === false || !fitsStay ? `Staying ${stay} minutes would miss the last train home.` : "");
      const lastLeave = Date.parse(data.latest_leave_iso);
      const deadline = Number.isFinite(closing) ? Math.min(closing, lastLeave) : lastLeave;
      showStayWarning([closingWarning, trainWarning].filter(Boolean).join(" "),
        closesEarly || !fitsStay ? shorterStay(deadline) : undefined);
      const depart = formatTime(data.last_train_depart_iso);
      element("p", `${fitsStay ? "Leave by" : "Last T option · leave by"} ${formatTime(data.latest_leave_iso)} · Home ${formatTime(last.summary.arrive_iso)}`, option);
      if (data.last_train_from && depart) element("p", `Last T from ${data.last_train_from} at ${depart}`, option);
      const details = element("details", "", option);
      element("summary", "Return steps", details);
      showSteps(details, last);
      updateTimelineAlerts();
    });

  if (directionPlace === place && locationPoint === point && !directionRequests.has("home") && !byId("homeRetry").hidden) {
    if (!renderWalkingHome()) showLastTrain(null, timingClock, "Schedules unavailable");
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
    element("div", [stop.lines.join(", "), `${stop.distance_km.toFixed(2)} km`, `${stop.walk_min} min walk`].filter(Boolean).join(" · "), item);
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
  walkingReturn = undefined;
  byId("directions").hidden = false;
  setText("longWalkNote", "");
  setText("rideNote", "");
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
  for (const id of ["routeLinks", "homeLinks", "tripTabs", "tripOptions", "homeOption", "stops", "timelineAlerts"]) byId(id).replaceChildren();
  byId("tripTabs").hidden = true;
  byId("homeWarning").hidden = true;
  byId("homeWarning").querySelector("details").open = false;
  link(byId("routeLinks"), "Open in Maps", routeMaps(place, locationPoint));
  if (!hasCoordinates(place)) setText("nearbyStatus", "Map coordinates are unavailable for this place.");
  byId("refreshTimes").hidden = !locationPoint || !hasCoordinates(place);
  if (locationPoint && hasCoordinates(place)) {
    link(byId("homeLinks"), "Maps home", routeMaps(locationPoint, place));
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
loadStations();
