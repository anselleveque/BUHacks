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
let tripTick;
let homeTick;
let nearbyTick;
let nearbyRequestedAt = -Infinity;
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
    if (options.map && options.map.tile_url) {
      map.attributionControl.removeAttribution(tiles.options.attribution);
      tiles.options.attribution = options.map.attribution;
      tiles.options.maxZoom = options.map.max_zoom || 19;
      map.attributionControl.addAttribution(options.map.attribution);
      tiles.setUrl(options.map.tile_url);
    }
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
  actions.className += " backup";
  link(actions, "Open in Maps", mapsLink(place));
  const button = element("button", "Directions", actions);
  button.type = "button";
  button.addEventListener("click", () => {
    showDirections(place);
    byId("directionsHeading").focus();
  });
  const openLate = place.open_late === true;
  const pin = marker(place, picked ? "picked" : openLate ? "late" : "", picked ? "★" : openLate ? "L" : "•")
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
    setText("mapStatus", data.places.length ? "L marks a place open past midnight. A star marks your pick." : "No places in this map area.");
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

function showDish(dish, clock) {
  const { place } = dish;
  setText("dish", dish.name);
  const note = typeof dish.price_note === "string" ? dish.price_note.trim() : "";
  const hasSizes = /^sizes:/i.test(note);
  const sizes = hasSizes ? note.slice("sizes:".length).trim() : "";
  const startingPrice = hasSizes || (typeof dish.notes === "string" && dish.notes.trim().toLowerCase() === "from");
  const price = byId("price");
  price.textContent = `${startingPrice ? "from " : ""}${money.format(dish.price)}`;
  price.title = sizes ? `Sizes: ${sizes}` : "";
  const details = byId("dishDetails");
  details.replaceChildren();
  element("p", place.name, details);
  if (sizes) element("p", price.title, details).className = "hint price-sizes";
  if (place.stop_line) element("p", place.stop_line, details);
  if (place.address) element("p", place.address, details);
  element("p", place.hours_text || "Hours unavailable", details);
  const badges = element("div", "", details);
  badges.className = "badges";
  if (place.open_now === true) element("span", "Open now", badges);
  if (place.open_late === true) element("span", "Open late", badges);
  if (dish.price_status === "verify") element("span", "price may vary", badges);
  for (const tag of dish.tags) element("span", tag, badges);
  const links = element("div", "", details);
  links.className = "links";
  showClosing(place, clock);
  link(links, "Menu source", dish.source_url);
  byId("result").hidden = false;
  byId("dish").focus();
  highlight(place);
  showDirections(place);
}

const relaxedLabels = {
  late: "late night", craving: "cravings", open_now: "open now", max_walk_min: "walking distance", area: "area", line: "line"
};

async function spin(reroll) {
  if (byId("spin").disabled) return;
  const previousPlace = directionPlace;
  cancelDirections();
  directionPlace = null;
  nearbyTick = null;
  byId("directions").hidden = true;
  if (!reroll) shown.clear();
  const fields = new FormData(form);
  const params = { budget: fields.get("budget"), open_now: fields.get("open_now") || "0" };
  if (fields.has("late")) Object.assign(params, { late: "1", min_minutes: 45 });
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
    const { dish, message, relaxed, now_iso } = await timedGet("/api/pick", params);
    const notice = !message && relaxed.length
      ? `Relaxed filters: ${relaxed.map((key) => relaxedLabels[key] ?? key).join(", ")}.` : "";
    setText("status", message || notice || (dish ? "" : "No dishes matched your preferences."));
    if (dish) {
      shown.add(dish.item_id);
      showDish(dish, responseClock(now_iso));
    } else {
      byId("result").hidden = true;
      highlight(null);
      directionPlace = null;
      closingTick = tripTick = homeTick = null;
      routeLayer.clearLayers();
      byId("directions").hidden = true;
    }
  } catch {
    setText("status", "A dish couldn’t be picked right now. Please try again.");
    if (previousPlace) showDirections(previousPlace);
  } finally {
    byId("spin").disabled = false;
    byId("reroll").disabled = false;
    byId("reroll").hidden = !shown.size;
  }
}

const bostonTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "numeric", minute: "2-digit"
});
const bostonHour = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hour: "numeric", hourCycle: "h23"
});
const demoTime = new URLSearchParams(window.location.search).get("now");
const demoParts = /^(?:\d{4}-\d{2}-\d{2}T)?([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/.exec(demoTime);
let demoClock;

function formatTime(iso) {
  const time = Date.parse(iso);
  return Number.isFinite(time) ? bostonTime.format(time).replace(/\s/g, " ") : "";
}

function withDemoTime(params) {
  return demoTime === null ? params : { ...params, now: demoTime };
}

function timedGet(path, params) {
  return get(path, withDemoTime(params), 30000);
}

function responseClock(iso) {
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return Date.now;
  const start = performance.now();
  const clock = () => time + performance.now() - start;
  // Requests using the same frozen demo instant share elapsed time across panels.
  if (demoParts) {
    if (demoClock?.iso !== iso) demoClock = { iso, clock };
    return demoClock.clock;
  }
  return clock;
}

function updateSuggestion() {
  const hour = demoParts ? Number(demoParts[1]) : Number(bostonHour.format(Date.now()));
  byId("lateSuggestion").hidden = byId("lateMode").checked || (hour < 21 && hour >= 5);
}

if (demoTime !== null) {
  byId("demoTime").hidden = false;
  setText("nearbyHeading", "Live departures (real time, not demo time)");
  if (demoParts) {
    const hour = Number(demoParts[1]);
    setText("demoTime", `Demo time ${hour % 12 || 12}:${demoParts[2]} ${hour < 12 ? "AM" : "PM"}`);
  } else {
    setText("demoTime", "Demo time is invalid. Use HH:MM.");
  }
}

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

function showClosing(place, clock) {
  const node = byId("closing");
  const time = place.closes_at_text || formatTime(place.open_until_iso);
  const hasDeadline = Number.isFinite(Date.parse(place.open_until_iso));
  node.hidden = !time || (!hasDeadline && place.closes_in_min == null);
  setText("closingLive", "");
  node.classList.remove("soon", "urgent");
  closingTick = null;
  if (node.hidden) return;
  const label = `Open until ${time}`;
  if (!hasDeadline) {
    node.textContent = `${label} · closes in ${duration(place.closes_in_min)}`;
    return;
  }
  const tick = countdown(node, byId("closingLive"), place.open_until_iso, clock,
    (ms, left) => ms <= 0 ? `Closed at ${time}` : `${label} · closes in ${left}`);
  closingTick = () => {
    const ms = Date.parse(place.open_until_iso) - clock();
    node.classList.toggle("urgent", ms < 600000);
    node.classList.toggle("soon", ms >= 600000 && ms < 1800000);
    tick();
  };
  closingTick();
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

const lineColor = (leg) => /^#[0-9a-f]{6}$/i.test(leg.color) ? leg.color : "#4d2a9a";

function showSteps(parent, itinerary) {
  const steps = element("ol", "", parent);
  steps.className = "steps";
  let previousRide;
  for (const leg of itinerary.legs) {
    const step = element("li", "", steps);
    if (leg.mode === "walk") {
      const distance = leg.distance_m < 1000 ? `${leg.distance_m} m` : `${(leg.distance_m / 1000).toFixed(2)} km`;
      const icon = element("span", "🚶 ", step);
      icon.setAttribute("aria-hidden", "true");
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
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 16 });
}

function showTripOptions(itineraries, clock, onSelect) {
  const tabs = byId("tripTabs");
  const options = byId("tripOptions");
  tabs.replaceChildren();
  options.replaceChildren();
  tabs.hidden = itineraries.length < 2;
  tripTick = null;
  if (!itineraries.length) {
    routeLayer.clearLayers();
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
    onSelect(itineraries[index].summary.arrive_iso);
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
    timers.push(countdown(node, live, summary.leave_by_iso, clock, (ms, left) => {
      if (ms < 0) {
        const next = itineraries.slice(index + 1).some((option) =>
          Date.parse(option.summary.leave_by_iso) >= clock());
        return next ? "Missed it, next option" : "Missed it";
      }
      if (ms <= 60000) return "Leave now";
      return `Leave in ${left}`;
    }));
    element("p", `${summary.duration_min} min total · Arrive ${formatTime(summary.arrive_iso)}`, panel);
    showSteps(panel, itinerary);
    panels.push(panel);
    if (itineraries.length === 1) continue;
    const tab = element("button", `Option ${index + 1}`, tabs);
    tab.type = "button";
    tab.id = `trip-tab-${index}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.tabIndex = 0;
    tab.addEventListener("click", () => select(index));
    tab.addEventListener("keydown", (event) => {
      const last = itineraries.length - 1;
      const next = { ArrowLeft: index ? index - 1 : last, ArrowRight: index === last ? 0 : index + 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(next);
      tabs.children[next].focus();
    });
  }
  select(0);
}

function cancelDirections() {
  for (const controller of directionRequests.values()) controller.abort();
  directionRequests.clear();
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
    retry.hidden = !(data.status === "unavailable" ||
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

async function showTrip(place, point) {
  showTripOptions([]);
  byId("routeStatus").classList.remove("banner", "warning");
  const handled = await loadDirection("route", "/api/trip", withDemoTime(tripParams(point, place)),
    "Planning your trip…", "Trip planning couldn’t load.", (data, walkingFallback) => {
      if (walkingFallback) byId("routeStatus").classList.add("banner", "warning");
      const itineraries = data.itineraries.slice(0, 3);
      setText("routeStatus", data.message || (itineraries.length ? "" : "No trip options are available."));
      if (itineraries.length) {
        showTripOptions(itineraries, responseClock(data.now_iso), (arrival) => {
          selectedArrival = arrival;
          showHome(place, point);
        });
      }
    });
  // With no selected trip, let the home endpoint estimate arrival.
  if (handled && directionPlace === place && locationPoint === point && selectedArrival === undefined) {
    selectedArrival = null;
    return showHome(place, point);
  }
}

function showHome(place, point) {
  homeTick = null;
  const option = byId("homeOption");
  option.replaceChildren();
  byId("homeWarning").hidden = true;
  const params = withDemoTime({ ...tripParams(place, point), stay_min: byId("stay").value });
  if (selectedArrival) params.arrive_iso = selectedArrival;
  if (place.open_until_iso) params.closes_iso = place.open_until_iso;
  return loadDirection("home", "/api/home", params,
    "Looking for the last train home…", "The last train home couldn’t load.", (data) => {
      const last = data.last_option;
      setText("homeStatus", data.message || (last ? "" : "No return itinerary was provided."));
      if (data.warning || data.stay_ok === false && last) {
        byId("homeWarning").hidden = false;
        setText("homeWarning", data.warning || `If you stay ${params.stay_min} minutes you will miss the last train home.`);
      }
      if (!last) return;
      element("h4", "Last train home", option);
      const departure = formatTime(data.last_train_depart_iso);
      if (data.last_train_from && departure) {
        element("p", `Last train leaves ${data.last_train_from} at ${departure}`, option);
      }
      const time = formatTime(data.latest_leave_iso);
      const node = element("p", "", option);
      node.className = "countdown";
      const live = element("span", "", option);
      live.className = "sr-only";
      live.setAttribute("role", "status");
      homeTick = countdown(node, live, data.latest_leave_iso, responseClock(data.now_iso),
        (ms, left) => `Leave the restaurant by ${time} (${ms < 0 ? "last train missed" : ms <= 1000 ? "leave now" : `in ${left}`})`);
      homeTick();
      element("p", `${last.summary.duration_min} min total · Arrive ${formatTime(last.summary.arrive_iso)}`, option);
      showSteps(option, last);
    });
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
  if (document.hidden || !directionPlace || !point || directionRequests.has("nearby")) return;
  nearbyRequestedAt = performance.now();
  return loadDirection("nearby", "/api/nearby", { lat: point.lat, lng: point.lng },
    background ? null : "Loading nearby stops and departures…", "Nearby stops couldn’t load.", renderNearby);
}

function showDirections(place) {
  cancelDirections();
  directionPlace = place;
  selectedArrival = undefined;
  byId("directions").hidden = false;
  setText("directionsHeading", `Directions to ${place.name}`);
  byId("routeStatus").classList.remove("banner", "warning");
  routeLayer.clearLayers();
  tripTick = homeTick = nearbyTick = null;
  setText("nearbyLive", "");
  for (const panel of ["route", "home", "nearby"]) {
    byId(`${panel}Loading`).hidden = true;
    byId(`${panel}Retry`).hidden = true;
  }
  for (const id of ["routeLinks", "homeLinks", "tripTabs", "tripOptions", "homeOption", "stops"]) byId(id).replaceChildren();
  byId("tripTabs").hidden = true;
  byId("homeWarning").hidden = true;
  byId("stay").disabled = !locationPoint || !hasCoordinates(place);
  link(byId("routeLinks"), "Open in Maps", mapsLink(place, locationPoint));
  if (!locationPoint) {
    setText("routeStatus", "Use your location or choose a starting station for directions.");
    setText("homeStatus", "Set a starting location to plan your way home.");
    setText("nearbyStatus", "Set a starting location to see nearby T stops.");
    return;
  }
  link(byId("homeLinks"), "Open in Maps", mapsLink(locationPoint, place));
  if (hasCoordinates(place)) {
    setText("homeStatus", "Waiting for the outbound arrival time…");
    return Promise.allSettled([
      showTrip(place, locationPoint),
      showNearby(locationPoint)
    ]);
  }
  setText("routeStatus", "Map coordinates are unavailable for this place.");
  setText("homeStatus", "A return trip needs the restaurant’s map coordinates.");
  return showNearby(locationPoint);
}

byId("stay").addEventListener("change", () => {
  if (selectedArrival !== undefined) showHome(directionPlace, locationPoint);
});
byId("routeRetry").addEventListener("click", () => showTrip(directionPlace, locationPoint));
byId("homeRetry").addEventListener("click", () => showHome(directionPlace, locationPoint));
byId("nearbyRetry").addEventListener("click", () => showNearby(locationPoint));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    const request = directionRequests.get("nearby");
    request?.abort();
    if (request) {
      directionRequests.delete("nearby");
      byId("nearbyLoading").hidden = true;
    }
  } else {
    nearbyTick?.();
    showNearby(locationPoint, Boolean(nearbyTick));
  }
});
byId("lateMode").addEventListener("change", updateSuggestion);
updateSuggestion();
setInterval(() => {
  updateSuggestion();
  closingTick?.();
  tripTick?.();
  homeTick?.();
  if (!document.hidden) {
    nearbyTick?.();
    if (performance.now() - nearbyRequestedAt >= 30000) showNearby(locationPoint, true);
  }
}, 1000);

budget.addEventListener("input", budgetLabel);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  spin(false);
});
byId("reroll").addEventListener("click", () => spin(true));
byId("retryOptions").addEventListener("click", loadOptions);
byId("retryPlaces").addEventListener("click", schedulePlaces);
map.on("moveend", schedulePlaces);
schedulePlaces();
loadOptions();
loadStations();
