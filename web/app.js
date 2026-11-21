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
let directionVersion = 0;
let nearbyRequest;
let userMarker;
let pickedMarker;
let placesTimer;
let placesVersion = 0;

function element(tag, value, parent) {
  const node = document.createElement(tag);
  node.textContent = value;
  parent.append(node);
  return node;
}

function link(parent, label, value) {
  if (!value) return;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return;
    const anchor = element("a", label, parent);
    anchor.href = url.href;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
  } catch {
    // A missing or malformed source URL has no usable link.
  }
}

async function get(path, params = {}, timeout = 15000) {
  const url = new URL(path, window.location.href);
  url.search = new URLSearchParams(params);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Request failed.");
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function setText(id, value) {
  byId(id).textContent = value ?? "";
}

function budgetLabel() {
  setText("budgetValue", `$${budget.value}`);
  budget.setAttribute("aria-valuetext", `${budget.value} dollars`);
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

async function loadOptions() {
  byId("retryOptions").hidden = true;
  setText("optionsStatus", "Loading preferences…");
  try {
    const options = await get("/api/options");
    budget.min = options.slider.min;
    budget.max = options.slider.max;
    budget.step = options.slider.step;
    budget.value = 12;
    setText("budgetMin", `$${budget.min}`);
    setText("budgetMax", `$${budget.max}`);
    budgetLabel();
    addChoices("diets", "diet", options.diets);
    addChoices("cravings", "craving", options.cravings);
    for (const line of options.lines) byId("lineFilter").add(new Option(line, line));
    for (const area of options.areas) byId("area").add(new Option(area, area));
    for (const minutes of options.walk_options) {
      walk.add(new Option(minutes === null ? "Anywhere on the T" : `Up to ${minutes} min`, minutes ?? ""));
    }
    walk.value = "";
    walk.disabled = !locationPoint;
    byId("filters").disabled = false;
    byId("spin").disabled = false;
    setText("optionsStatus", "");
  } catch {
    setText("optionsStatus", "Preferences couldn’t load. Please try again.");
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

const map = L.map("map").setView([42.351, -71.093], 13);
const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);
tiles.on("tileerror", () => {
  setText("tileStatus", "Some map background tiles couldn’t load.");
});
tiles.on("loading", () => setText("tileStatus", ""));
const placeMarkers = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const hasCoordinates = (place) => Number.isFinite(place?.lat) && Number.isFinite(place?.lng);
const coordinates = (place) => [place.lat, place.lng];

function marker(place, style, symbol) {
  const pin = L.marker(coordinates(place), {
    icon: L.divIcon({ className: `pin ${style}`, html: symbol, iconSize: [26, 26] }),
    title: place.name,
    alt: place.name,
    zIndexOffset: style === "picked" ? 1000 : style === "user" ? 500 : 0
  });
  pin.on("add", () => pin.getElement().setAttribute("aria-label", place.name));
  return pin;
}

function placeMarker(place, picked = false) {
  const popup = document.createElement("div");
  element("strong", place.name, popup);
  element("p", place.hours_text || "Hours unavailable", popup);
  const actions = element("div", "", popup);
  actions.className = "links";
  link(actions, "Google Maps", place.maps_url);
  const button = element("button", "Directions", actions);
  button.type = "button";
  button.addEventListener("click", () => {
    showDirections(place);
    byId("directionsHeading").focus();
  });
  const pin = marker(place, picked ? "picked" : place.late_night ? "late" : "", picked ? "★" : place.late_night ? "L" : "•")
    .bindPopup(popup);
  pin.placeId = place.id;
  return pin;
}

async function loadPlaces(version) {
  const center = map.getCenter();
  const bounds = map.getBounds();
  const radius = Math.max(center.distanceTo(bounds.getNorthEast()), center.distanceTo(bounds.getSouthWest())) / 1000;
  byId("retryPlaces").hidden = true;
  try {
    const data = await get("/api/places", { lat: center.lat, lng: center.lng, radius_km: radius });
    if (version !== placesVersion) return;
    placeMarkers.clearLayers();
    for (const place of data.places) {
      if (hasCoordinates(place) && place.id !== pickedPlace?.id) placeMarker(place).addTo(placeMarkers);
    }
    setText("mapStatus", data.places.length ? "L marks a late-night place. A star marks your pick." : "No places in this map area.");
  } catch {
    if (version !== placesVersion) return;
    setText("mapStatus", "Places couldn’t load for this map area. Try again or move the map.");
    byId("retryPlaces").hidden = false;
  }
}

function schedulePlaces() {
  clearTimeout(placesTimer);
  const version = ++placesVersion;
  placesTimer = setTimeout(() => loadPlaces(version), 200);
}

function setLocation(point) {
  locationPoint = point;
  walk.disabled = !point;
  userMarker?.remove();
  if (point) {
    userMarker = marker(point, "user", "●").addTo(map);
    map.setView(coordinates(point), Math.max(map.getZoom(), 13));
  } else {
    walk.value = "";
  }
  setText("locationStatus", point ? `Starting from ${point.name}.` : "");
  if (directionPlace) showDirections(directionPlace);
}

station.addEventListener("change", () => {
  locationVersion++;
  byId("locate").disabled = false;
  setLocation(stations.get(station.value));
});
byId("locate").addEventListener("click", () => {
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

function highlight(place) {
  pickedMarker?.remove();
  pickedPlace = place;
  if (hasCoordinates(place)) {
    placeMarkers.eachLayer((pin) => {
      if (pin.placeId === place.id) placeMarkers.removeLayer(pin);
    });
    pickedMarker = placeMarker(place, true).addTo(map);
    if (!map.getBounds().contains(coordinates(place))) map.panTo(coordinates(place));
  }
  schedulePlaces();
}

function showDish(dish) {
  const { place } = dish;
  setText("dish", dish.name);
  setText("price", money.format(dish.price));
  const details = byId("dishDetails");
  details.replaceChildren();
  element("p", place.name, details);
  if (place.stop_line) element("p", place.stop_line, details);
  if (place.address) element("p", place.address, details);
  element("p", place.hours_text || "Hours unavailable", details);
  const badges = element("div", "", details);
  badges.className = "badges";
  if (place.open_now) element("span", "Open now", badges);
  if (place.open_late) element("span", "Open late", badges);
  if (dish.price_status === "verify") element("span", "price may vary", badges);
  for (const tag of dish.tags) element("span", tag, badges);
  const links = element("div", "", details);
  links.className = "links";
  link(links, "Google Maps", place.maps_url);
  link(links, "Menu source", dish.source_url);
  byId("result").hidden = false;
  byId("dish").focus();
  highlight(place);
  showDirections(place);
}

const relaxedLabels = {
  craving: "cravings", open_now: "open now", max_walk_min: "walking distance", area: "area", line: "line"
};

async function spin(reroll) {
  if (byId("spin").disabled) return;
  if (!reroll) shown.clear();
  const fields = new FormData(form);
  const params = { budget: fields.get("budget"), open_now: fields.get("open_now") || "0" };
  for (const name of ["diet", "craving", "line", "area", "max_walk_min"]) {
    const value = fields.getAll(name).join(",");
    if (value) params[name] = value;
  }
  if (locationPoint) Object.assign(params, { lat: locationPoint.lat, lng: locationPoint.lng });
  if (reroll && shown.size) params.exclude = [...shown].join(",");
  byId("spin").disabled = true;
  byId("reroll").disabled = true;
  setText("status", "Looking for a dish…");
  try {
    const { dish, message, relaxed } = await get("/api/pick", params);
    const notice = relaxed.length ? `Relaxed filters: ${relaxed.map((key) => relaxedLabels[key] ?? key).join(", ")}.` : "";
    setText("status", [message || (!dish ? "No dishes matched your preferences." : ""), notice].filter(Boolean).join(" "));
    if (dish) {
      shown.add(dish.item_id);
      showDish(dish);
    } else {
      byId("result").hidden = true;
      highlight(null);
      directionPlace = null;
      directionVersion++;
      routeLayer.clearLayers();
      byId("directions").hidden = true;
    }
  } catch {
    setText("status", "A dish couldn’t be picked right now. Please try again.");
  } finally {
    byId("spin").disabled = false;
    byId("reroll").disabled = false;
    byId("reroll").hidden = !shown.size;
  }
}

async function showRoute(place, point, version) {
  if (!hasCoordinates(place)) {
    setText("routeStatus", "Map coordinates are unavailable for this place.");
    return;
  }
  setText("routeStatus", "Loading the walking route…");
  try {
    const route = await get("/api/route", {
      from_lat: point.lat, from_lng: point.lng, to_lat: place.lat, to_lng: place.lng
    });
    if (version !== directionVersion) return;
    const links = byId("routeLinks");
    link(links, "Google transit", route.links.google_transit);
    link(links, "Google walking", route.links.google_walking);
    link(links, "Apple transit", route.links.apple_transit);
    if (!route.ok) {
      setText("routeStatus", route.message || "Walking routing is unavailable right now.");
      byId("retryDirections").hidden = false;
      return;
    }
    const distance = route.distance_m < 1000 ? `${route.distance_m} m` : `${(route.distance_m / 1000).toFixed(2)} km`;
    setText("routeStatus", `${distance} · ${Math.ceil(route.duration_s / 60)} min walk`);
    const path = L.polyline(route.geometry, { color: "#4d2a9a", weight: 5 }).addTo(routeLayer);
    if (path.getBounds().isValid() && !map.getBounds().contains(path.getBounds())) {
      map.fitBounds(path.getBounds(), { padding: [30, 30], maxZoom: 16 });
    }
  } catch {
    if (version === directionVersion) {
      setText("routeStatus", "Directions couldn’t load. Please try again.");
      byId("retryDirections").hidden = false;
    }
  }
}

async function showNearby(point, version) {
  setText("nearbyStatus", "Loading nearby stops and departures…");
  const key = coordinates(point).join(",");
  if (nearbyRequest?.key !== key || Date.now() - nearbyRequest.time >= 30000) {
    const request = { key, time: Date.now(), data: get("/api/nearby", { lat: point.lat, lng: point.lng }) };
    nearbyRequest = request;
    request.data.catch(() => {
      if (nearbyRequest === request) nearbyRequest = null;
    });
  }
  try {
    const { stops, message } = await nearbyRequest.data;
    if (version !== directionVersion) return;
    setText("nearbyStatus", message || (stops.length ? "" : "No nearby T stops were found."));
    for (const stop of stops) {
      const item = element("li", "", byId("stops"));
      element("strong", stop.name, item);
      element("div", [stop.lines.join(", "), `${stop.distance_km.toFixed(2)} km`, `${stop.walk_min} min walk`].filter(Boolean).join(" · "), item);
      const departures = stop.departures.map(({ line, minutes }) => `${line}: ${minutes <= 0 ? "due now" : `${minutes} min`}`);
      element("div", departures.join(" · ") || "No upcoming departures available.", item);
    }
  } catch {
    if (version === directionVersion) {
      setText("nearbyStatus", "Nearby stops couldn’t load. Please try again.");
      byId("retryDirections").hidden = false;
    }
  }
}

function showDirections(place) {
  directionPlace = place;
  const version = ++directionVersion;
  byId("directions").hidden = false;
  byId("retryDirections").hidden = true;
  setText("directionsHeading", `Directions to ${place.name}`);
  routeLayer.clearLayers();
  byId("routeLinks").replaceChildren();
  byId("stops").replaceChildren();
  if (!locationPoint) {
    setText("routeStatus", "Use your location or choose a starting station for directions.");
    setText("nearbyStatus", "Set a starting location to see nearby T stops.");
    return;
  }
  showRoute(place, locationPoint, version);
  showNearby(locationPoint, version);
}

budget.addEventListener("input", budgetLabel);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  spin(false);
});
byId("reroll").addEventListener("click", () => spin(true));
byId("retryOptions").addEventListener("click", loadOptions);
byId("retryPlaces").addEventListener("click", schedulePlaces);
byId("retryDirections").addEventListener("click", () => showDirections(directionPlace));
map.on("moveend", schedulePlaces);
schedulePlaces();
loadOptions();
loadStations();
