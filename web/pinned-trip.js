"use strict";

const pinnedKey = "midnight-munch-pinned-trip";
const pinVersion = 1;
const pinnedDialog = byId("pinnedTripDialog");
const replaceDialog = byId("replacePinnedDialog");
const pinnedPane = map.createPane("pinnedRoutes");
pinnedPane.style.zIndex = "430";
const pinnedLayer = L.layerGroup().addTo(map);
const pinTime = (iso) => Date.parse(iso);
const pinText = (value) => typeof value === "string" && value.trim().length > 0 && value.length <= 240;
const validCoordinate = ([lat, lng]) => Number.isFinite(lat) && Math.abs(lat) <= 90 &&
  Number.isFinite(lng) && Math.abs(lng) <= 180;

function snapshotPoint(point) {
  if (!pinText(point?.name) || !validCoordinate([point.lat, point.lng])) return null;
  const { name, lat, lng } = point;
  return { name, lat, lng, stop_id: pinText(point.stop_id) ? point.stop_id : undefined };
}

function snapshotItinerary(itinerary) {
  const summary = itinerary?.summary;
  const depart = pinTime(summary?.depart_iso), arrive = pinTime(summary?.arrive_iso);
  if (!Number.isFinite(depart) || !Number.isFinite(arrive) || arrive < depart || arrive - depart > 86400000 ||
      !Array.isArray(itinerary.legs) || !itinerary.legs.length || itinerary.legs.length > 20) return null;
  const legs = [];
  let earliest = depart;
  for (const leg of itinerary.legs) {
    const from = snapshotPoint(leg?.from), to = snapshotPoint(leg?.to);
    if (!from || !to || !Array.isArray(leg.geometry) || leg.geometry.length < 2 || leg.geometry.length > 10000 ||
        !leg.geometry.every((point) => Array.isArray(point) && point.length === 2 && validCoordinate(point))) return null;
    const geometry = leg.geometry.map(([lat, lng]) => [lat, lng]);
    if (leg.mode === "walk" && Number.isFinite(leg.duration_min) && leg.duration_min >= 0 && leg.duration_min <= 1440) {
      const walkDepart = pinTime(leg.depart_iso);
      const walkArrival = pinTime(leg.arrive_iso);
      legs.push({ mode: "walk", from, to, geometry, duration_min: leg.duration_min,
        estimated: leg.estimated === true, distance_m: Number.isFinite(leg.distance_m) ? leg.distance_m : 0,
        depart_iso: Number.isFinite(walkDepart) ? leg.depart_iso : undefined,
        arrive_iso: Number.isFinite(walkArrival) ? leg.arrive_iso : undefined });
    } else if (leg.mode === "ride" && pinText(leg.line)) {
      const departure = pinTime(leg.depart_iso), arrival = pinTime(leg.arrive_iso);
      if (!Number.isFinite(departure) || !Number.isFinite(arrival) || departure < earliest ||
          arrival < departure || arrival > arrive) return null;
      earliest = arrival;
      legs.push({ mode: "ride", from, to, geometry, line: leg.line, route_id: pinText(leg.route_id) ? leg.route_id : undefined,
        direction_name: pinText(leg.direction_name) ? leg.direction_name : to.name,
        vehicle: leg.vehicle === "bus" ? "bus" : "subway", color: lineColor(leg),
        depart_iso: leg.depart_iso, arrive_iso: leg.arrive_iso,
        stops: Number.isInteger(leg.stops) && leg.stops >= 0 ? leg.stops : null });
    } else {
      return null;
    }
  }
  const fares = {};
  for (const key of ["fare", "fare_each", "fare_trips"]) {
    if (Number.isFinite(itinerary[key]) && itinerary[key] >= 0) fares[key] = itinerary[key];
  }
  return { ...fares, mode: legs.some((leg) => leg.mode === "ride") ? "transit" : "walk", legs, summary: { depart_iso: summary.depart_iso, arrive_iso: summary.arrive_iso,
    leave_by_iso: Number.isFinite(pinTime(summary.leave_by_iso)) ? summary.leave_by_iso : summary.depart_iso } };
}

function validatePinned(value) {
  if (value?.version !== pinVersion || !Number.isInteger(value.place?.id) ||
      !Number.isInteger(value.stay) || value.stay < 0 || value.stay > 180 ||
      !Number.isInteger(value.through) || value.through < -1 || value.through > 90 ||
      !Number.isFinite(value.anchor?.wall) || !Number.isFinite(value.anchor?.time) ||
      typeof value.anchor.demo !== "boolean") return null;
  const place = snapshotPoint(value.place), start = snapshotPoint(value.start);
  const outbound = snapshotItinerary(value.outbound);
  const home = value.home == null ? null : snapshotItinerary(value.home);
  if (!place || !start || !outbound || value.home != null && !home ||
      home && pinTime(home.summary.depart_iso) < pinTime(outbound.summary.arrive_iso)) return null;
  if (value.dish != null && (!pinText(value.dish.name) || !Number.isInteger(value.dish.id))) return null;
  const fare = {};
  for (const key of ["fare", "fare_each", "fare_trips", "price", "total_cost"]) {
    if (Number.isFinite(value.fare?.[key]) && value.fare[key] >= 0) fare[key] = value.fare[key];
  }
  const snapshot = {
    version: pinVersion, late: value.late === true, place: { ...place, id: value.place.id,
      open_until_iso: Number.isFinite(pinTime(value.place.open_until_iso)) ? value.place.open_until_iso : null },
    start, outbound, home, stay: value.stay, through: value.through, fare,
    dish: value.dish ? { name: value.dish.name, id: value.dish.id } : null,
    anchor: { wall: value.anchor.wall, time: value.anchor.time, demo: value.anchor.demo }
  };
  return snapshot.through < buildPinnedSteps(snapshot).length - 1 ? snapshot : null;
}

function pinnedNow(trip) {
  return trip.anchor.demo ? trip.anchor.time + Math.max(0, Date.now() - trip.anchor.wall) : Date.now();
}

function pinnedExpired(trip) {
  const end = pinTime(trip.home?.summary.arrive_iso ?? trip.outbound.summary.arrive_iso);
  return pinnedNow(trip) >= end + (trip.home ? 30 : trip.stay + 30) * 60000;
}

function readPinned() {
  try {
    const raw = localStorage.getItem(pinnedKey);
    if (raw === null) return null;
    const saved = raw.length < 600000 ? validatePinned(JSON.parse(raw)) : null;
    if (saved && !pinnedExpired(saved)) return saved;
  } catch {
    // A corrupt or inaccessible snapshot does not prevent the planner from loading.
  }
  try { localStorage.removeItem(pinnedKey); } catch { /* Storage may be unavailable. */ }
  return null;
}

let pinnedTrip = readPinned();
let pinnedSteps = [];
let activeStep = -1;
let lastStepKey = "";
let undoTrip;
let replacement;
let nextRoute;
let nextRequest;

function snapshotFromPlan(routeOnly) {
  if (!directionPlace || !locationPoint || !selectedTrip || kitchenClosed ||
      !catchable(selectedTrip, timingClock())) return null;
  const closing = pinTime(directionPlace.open_until_iso);
  if (Number.isFinite(closing) && pinTime(selectedTrip.summary.arrive_iso) >= closing) return null;
  const feasibleHome = selectedHomeTrip &&
    pinTime(selectedHomeTrip.summary.depart_iso) >= pinTime(selectedTrip.summary.arrive_iso) ? selectedHomeTrip : null;
  if (!routeOnly && !feasibleHome) return null;
  const dish = currentDish;
  const fare = { price: dish?.price };
  const shown = displayedFare(dish ?? {}, selectedTrip, feasibleHome);
  Object.assign(fare, { fare: shown.total, fare_each: shown.each, fare_trips: shown.trips,
    total_cost: dish ? dish.price + shown.total : undefined });
  return validatePinned({ version: pinVersion, late: planningLate(), place: directionPlace, start: locationPoint, outbound: selectedTrip,
    home: feasibleHome, stay: stayMinutes(), through: -1, fare,
    dish: dish ? { name: dish.name, id: dish.item_id } : null,
    anchor: { wall: Date.now(), time: timingClock(), demo: demoEpoch !== null } });
}

function samePinnedRoute(snapshot) {
  return Boolean(pinnedTrip && snapshot && snapshot.place.id === pinnedTrip.place.id &&
    snapshot.dish?.id === pinnedTrip.dish?.id && snapshot.start?.lat === pinnedTrip.start.lat &&
    snapshot.start?.lng === pinnedTrip.start.lng && (sameDeparture(snapshot.outbound, pinnedTrip.outbound) ||
      !rideLegs(snapshot.outbound).length && !rideLegs(pinnedTrip.outbound).length));
}

function samePinnedHome(home, stay) {
  if (!home || !pinnedTrip.home || stay !== pinnedTrip.stay) return false;
  return sameDeparture(home, pinnedTrip.home) || !rideLegs(home).length && !rideLegs(pinnedTrip.home).length;
}

function updatePinControls() {
  const showingPlan = !byId("selectedDetails").hidden && !byId("directions").hidden;
  const closing = pinTime(directionPlace?.open_until_iso);
  const canPin = showingPlan && selectedTrip && !kitchenClosed && catchable(selectedTrip, timingClock()) &&
    (!Number.isFinite(closing) || pinTime(selectedTrip.summary.arrive_iso) < closing);
  const same = canPin && samePinnedRoute({ place: directionPlace, dish: currentDish ? { id: currentDish.item_id } : null,
    start: locationPoint, outbound: selectedTrip });
  const sameNight = same && samePinnedHome(selectedHomeTrip, stayMinutes());
  const canPinNight = canPin && selectedHomeTrip &&
    pinTime(selectedHomeTrip.summary.depart_iso) >= pinTime(selectedTrip.summary.arrive_iso);
  for (const [id, available, label] of [["pinRoute", canPin, "Pin this route"], ["pinNight", canPinNight, "Pin my night"]]) {
    const button = byId(id);
    button.hidden = !available;
    const text = (id === "pinRoute" ? same : sameNight) ? "Pinned ✓" : label;
    if (button.textContent !== text) button.textContent = text;
  }
}

function legSteps(itinerary, section) {
  const steps = [];
  let cursor = pinTime(itinerary.summary.depart_iso);
  const finalArrival = pinTime(itinerary.summary.arrive_iso);
  for (const [index, leg] of itinerary.legs.entries()) {
    if (leg.mode === "walk") {
      const start = Number.isFinite(pinTime(leg.depart_iso)) ? Math.max(cursor, pinTime(leg.depart_iso)) : cursor;
      const nextRide = itinerary.legs.slice(index + 1).find(({ mode }) => mode === "ride");
      const end = Math.min(finalArrival, nextRide ? pinTime(nextRide.depart_iso) : finalArrival,
        Number.isFinite(pinTime(leg.arrive_iso)) ? pinTime(leg.arrive_iso) : start + leg.duration_min * 60000);
      if (leg.duration_min > 0) {
        steps.push({ section, type: "walk", icon: "walk", start, at: end,
          text: `Walk to ${leg.to.name} · ${walkTimeLabel(leg)}`, end });
      }
      cursor = Math.max(cursor, end);
    } else {
      const depart = pinTime(leg.depart_iso), arrive = pinTime(leg.arrive_iso);
      const vehicle = vehicleName(leg), line = rideLabel(leg);
      steps.push({ section, type: "board", icon: vehicle, start: Math.min(cursor, depart), at: depart, leg,
        text: section === "HOME" && !steps.some((step) => step.type === "board")
          ? `Last ${vehicle === "bus" ? line : "T"} from ${leg.from.name} at ${formatTime(leg.depart_iso)}`
          : `Board ${line} toward ${leg.direction_name} at ${leg.from.name}, ${formatTime(leg.depart_iso)}` });
      steps.push({ section, type: "ride", icon: vehicle, start: depart, at: arrive, leg,
        text: `On the ${vehicle}: exit at ${leg.to.name} ${formatTime(leg.arrive_iso)}`,
        detail: leg.stops == null ? "" : `${leg.stops} ${leg.stops === 1 ? "stop" : "stops"}` });
      cursor = arrive;
    }
  }
  return steps;
}

function buildPinnedSteps(trip) {
  const arrival = pinTime(trip.outbound.summary.arrive_iso);
  const leave = pinTime(trip.home?.summary.depart_iso);
  const closing = pinTime(trip.place.open_until_iso);
  const eatEnd = Math.min(arrival + trip.stay * 60000, Number.isFinite(leave) ? leave : Infinity,
    Number.isFinite(closing) ? Math.max(arrival, closing) : Infinity);
  const steps = legSteps(trip.outbound, "GO");
  const time = (ms) => formatTime(new Date(ms).toISOString());
  steps.push({ section: "EAT", type: "eat", icon: "eat", start: arrival, at: eatEnd,
    text: `Eating until ${time(eatEnd)}`,
    detail: eatEnd < arrival + trip.stay * 60000
      ? eatEnd === closing ? "The kitchen closes before the end of your planned stay." : "Leave early to catch the last ride." : "" });
  if (trip.home) {
    const vehicle = rideLegs(trip.home)[0];
    if (leave > eatEnd) steps.push({ section: "HOME", type: "leave", icon: vehicle ? vehicleName(vehicle) : "walk",
      start: eatEnd, at: leave, text: `Leave the restaurant by ${time(leave)}` });
    steps.push(...legSteps(trip.home, "HOME"));
    const homeAt = pinTime(trip.home.summary.arrive_iso);
    steps.push({ section: "HOME", type: "finish", icon: "pin", start: homeAt, at: homeAt,
      text: `Home ${time(homeAt)}` });
  }
  return steps;
}

function drawPinnedTrip() {
  pinnedLayer.clearLayers();
  if (!pinnedTrip) return;
  const marked = new Set();
  for (const itinerary of [pinnedTrip.outbound, pinnedTrip.home].filter(Boolean)) {
    for (const leg of itinerary.legs) {
      const geometry = leg.estimated ? [coordinates(leg.from), coordinates(leg.to)] : leg.geometry;
      const options = { pane: "pinnedRoutes", lineCap: "round", dashArray: leg.estimated ? "4 7" : leg.mode === "walk" ? "7 7" : null };
      L.polyline(geometry, { ...options, color: "#f6ffcf", weight: 11, interactive: false }).addTo(pinnedLayer);
      L.polyline(geometry, { ...options, color: leg.mode === "ride" ? lineColor(leg) : "#224b3b",
        weight: 7, className: "pinned-path" }).bindTooltip(iconText(leg.mode === "ride"
          ? `Pinned ${rideLabel(leg)}` : leg.estimated ? "Pinned walk (estimate)" : "Pinned walk")).addTo(pinnedLayer);
      if (leg.mode !== "ride") continue;
      for (const [verb, point] of [["Board", leg.from], ["Exit", leg.to]]) {
        const key = coordinates(point).join(",");
        if (marked.has(key)) continue;
        marked.add(key);
        const label = `Pinned: ${verb} ${rideLabel(leg)} at ${point.name}`;
        marker({ ...point, name: label }, "pinned-marker", makeIcon(vehicleName(leg))).setZIndexOffset(1400)
          .bindTooltip(iconText(label)).addTo(pinnedLayer);
      }
    }
  }
  marker({ ...pinnedTrip.place, name: `Pinned restaurant: ${pinnedTrip.place.name}` }, "pinned-marker",
    makeIcon("pin")).setZIndexOffset(1500).addTo(pinnedLayer);
}

function savePinned() {
  try {
    if (pinnedTrip) localStorage.setItem(pinnedKey, JSON.stringify(pinnedTrip));
    else localStorage.removeItem(pinnedKey);
  } catch {
    // An in-memory pin is still usable if browser storage is unavailable.
  }
}

function clearNextRoute() {
  nextRequest?.abort();
  nextRequest = nextRoute = undefined;
  byId("pinnedReplacement").hidden = true;
  byId("findPinnedNext").disabled = byId("barFindNext").disabled = false;
  setText("pinnedRequestStatus", "");
}

function setPinned(trip) {
  clearNextRoute();
  pinnedTrip = trip;
  pinnedSteps = trip ? buildPinnedSteps(trip) : [];
  activeStep = -1;
  lastStepKey = "";
  savePinned();
  drawPinnedTrip();
  updatePinControls();
  updatePinnedTrip();
}

function unpin(withUndo = false) {
  if (!pinnedTrip) return;
  const previous = pinnedTrip;
  const focusWasInBar = document.activeElement?.closest("#tripBar");
  undoTrip = withUndo ? { trip: previous, until: Date.now() + 8000 } : null;
  byId("tripUndo").hidden = !undoTrip;
  if (pinnedDialog.open) pinnedDialog.close();
  setPinned(null);
  if (withUndo) byId("undoUnpin").focus();
  else if (focusWasInBar) byId("planTab").focus();
  setText("tripAnnouncement", withUndo ? "Trip unpinned. Undo is available for eight seconds." : "Pinned trip ended.");
}

function stepProgress(trip, now) {
  let index = Math.max(0, trip.through + 1);
  for (let i = index; i < pinnedSteps.length; i++) {
    const step = pinnedSteps[i];
    if (step.type === "board" && now >= step.at) return { index: i, missed: true };
    if (now >= step.start) index = i;
    else break;
  }
  return { index, missed: false };
}

function renderPinnedSteps(index) {
  const list = byId("pinnedSteps");
  list.replaceChildren();
  for (const [i, step] of pinnedSteps.entries()) {
    const row = element("li", "", list);
    row.className = i < index ? "pinned-done" : "";
    if (i === index) row.setAttribute("aria-current", "step");
    const label = element("span", i < index ? "✓ " : "", row);
    label.className = "pinned-step-label";
    if (i < index) {
      const complete = element("span", "Completed. ", label);
      complete.className = "sr-only";
    }
    label.append(makeIcon(step.icon), " ", step.section);
    const detail = element("div", "", row);
    if (step.type === "board" || step.type === "ride") rideChip(step.leg, detail);
    element("div", step.text, detail);
    element("small", [formatTime(new Date(step.start).toISOString()), step.detail].filter(Boolean).join(" · "), detail);
  }
}

function updatePinnedTrip() {
  if (undoTrip && Date.now() >= undoTrip.until) {
    undoTrip = null;
    byId("tripUndo").hidden = true;
    if (document.activeElement === byId("undoUnpin")) byId("planTab").focus();
  }
  byId("tripBar").hidden = !pinnedTrip;
  if (!pinnedTrip) return;
  if (pinnedExpired(pinnedTrip)) {
    unpin();
    return;
  }
  const now = pinnedNow(pinnedTrip);
  const { index, missed } = stepProgress(pinnedTrip, now);
  const step = pinnedSteps[index];
  const line = step.leg ? rideLabel(step.leg) : "";
  const text = missed ? `You may have missed the ${formatTime(step.leg.depart_iso)} ${line}.` : step.text;
  const key = `${index}:${missed}`;
  if (lastStepKey !== key) {
    if (lastStepKey) clearNextRoute();
    lastStepKey = key;
    activeStep = index;
    const label = byId("tripNext");
    label.replaceChildren(makeIcon(step.icon), " ", text);
    setText("tripAnnouncement", missed ? `${text} Find the next one.` : text);
    byId("barFindNext").hidden = byId("findPinnedNext").hidden = !missed;
    setText("donePinnedStep", step.type === "board" ? `I'm on the ${step.icon}` : step.type === "walk" || step.type === "ride" ? "I arrived" : "Done");
    setText("pinnedPlace", [pinnedTrip.place.name, pinnedTrip.dish?.name].filter(Boolean).join(" · "));
    const fare = pinnedTrip.fare;
    setText("pinnedFare", Number.isFinite(fare.fare)
      ? fare.fare === 0 ? "Walkable, no fare" : `Planned fare ${money.format(fare.fare)}`
      : "");
    renderPinnedSteps(index);
  }
  const left = step.at - now;
  setText("tripCountdown", !missed && left > 0 ? ` · in ${remaining(left)}` : "");
}

function openPinnedTimeline(trigger = byId("openPinnedTrip")) {
  if (pinnedTrip && !pinnedDialog.open) openDialog(pinnedDialog, trigger);
}

function requestPin(routeOnly, trigger) {
  const snapshot = snapshotFromPlan(routeOnly);
  if (!snapshot) return;
  if (samePinnedRoute(snapshot) && (routeOnly || samePinnedHome(snapshot.home, snapshot.stay))) {
    openPinnedTimeline(trigger);
    return;
  }
  if (!pinnedTrip) {
    undoTrip = null;
    byId("tripUndo").hidden = true;
    setPinned(snapshot);
    return;
  }
  replacement = snapshot;
  setText("replacePinnedPlace", `Pin the route for ${snapshot.place.name} instead?`);
  openDialog(replaceDialog, trigger);
}

function tripNowParams(trip, params) {
  return { ...params, trips: trip.fare.fare_trips === 1 ? 1 : 2, ...(trip.late ? { late: 1 } : {}),
    ...(trip.anchor.demo ? { now: new Date(pinnedNow(trip)).toISOString() } : {}) };
}

async function nextPinnedHome(trip, outbound, controller) {
  const arrival = pinTime(outbound.summary.arrive_iso);
  if (walkOnly(trip.home ?? outbound)) {
    let leg;
    try {
      const route = await get("/api/route", tripParams(trip.place, trip.start), 8000, controller);
      if (route.ok) leg = walkLegFromRoute(route, trip.place, trip.start);
    } catch (error) {
      if (error.name === "AbortError") throw error;
    }
    leg ??= localWalkEstimate(trip.place, trip.start);
    const depart = Math.min(arrival + trip.stay * 60000, pinTime(trip.place.open_until_iso) || Infinity);
    return leg ? walkingItinerary(leg, depart, depart + leg.duration_min * 60000) : null;
  }
  const params = { ...tripParams(trip.place, trip.start), stay_min: trip.stay, arrive_iso: outbound.summary.arrive_iso };
  if (trip.place.open_until_iso) params.closes_iso = trip.place.open_until_iso;
  const home = await get("/api/home", tripNowParams(trip, params), 25000, controller);
  return home.last_option && pinTime(home.latest_leave_iso) >= arrival
    ? { ...home.last_option, fare: home.last_option.fare ?? home.last_option.fare_each ?? home.fare_each_transit, summary: { ...home.last_option.summary, depart_iso: home.latest_leave_iso } } : null;
}

async function findPinnedNext(trigger) {
  const trip = pinnedTrip;
  if (!trip || nextRequest) return;
  const step = pinnedSteps[activeStep];
  if (step?.type !== "board") return;
  openPinnedTimeline(trigger);
  byId("barFindNext").disabled = byId("findPinnedNext").disabled = true;
  byId("pinnedReplacement").hidden = true;
  setText("pinnedRequestStatus", "Finding the next route…");
  const controller = new AbortController();
  nextRequest = controller;
  try {
    const returning = step.section === "HOME";
    const board = step.leg.from;
    const origin = returning || pinnedSteps.slice(0, activeStep).some((prior) => prior.type === "board")
      ? board : trip.start;
    const destination = returning ? trip.start : trip.place;
    const data = await get("/api/trip", tripNowParams(trip, tripParams(origin, destination)), 25000, controller);
    if (nextRequest !== controller || pinnedTrip !== trip) return;
    const available = data.itineraries?.filter((option) => catchable(option, pinnedNow(trip)));
    const found = available?.find((option) => !walkOnly(option)) ?? available?.[0];
    if (!found) throw new Error(data.message || "No trains or buses are scheduled for this trip right now.");
    const route = snapshotItinerary(found);
    if (!route) throw new Error("The next route didn’t include usable times.");
    const closing = pinTime(trip.place.open_until_iso);
    if (!returning && Number.isFinite(closing) && pinTime(route.summary.arrive_iso) >= closing) {
      throw new Error("The next route arrives after the kitchen closes.");
    }
    const home = returning ? route : await nextPinnedHome(trip, route, controller);
    if (nextRequest !== controller || pinnedTrip !== trip) return;
    const outbound = returning ? trip.outbound : route;
    const fare = { ...trip.fare };
    const nextFare = displayedFare(fare, outbound, home, { trips: fare.fare_trips, each: data.fare_each_transit,
      walkHome: home ? walkOnly(home) : true });
    if (Number.isFinite(nextFare.total)) {
      Object.assign(fare, { fare: nextFare.total, fare_each: nextFare.each, total_cost: fare.price + nextFare.total });
    }
    const snapshot = validatePinned({ ...trip, fare, outbound, home,
      through: returning ? pinnedSteps.findIndex((entry) => entry.section === "HOME") - 1 : -1 });
    if (!snapshot) throw new Error("The next route didn’t include usable times.");
    nextRoute = snapshot;
    const routes = rideLegs(route).map(rideLabel).join(" → ") || "Walk";
    setText("pinnedReplacementSummary", `${routes} · Leave ${formatTime(route.summary.depart_iso)} · Arrive ${formatTime(route.summary.arrive_iso)}`);
    setText("pinnedRequestStatus", snapshot.home ? "" : "No return route was available for this option.");
    byId("pinnedReplacement").hidden = false;
    byId("confirmPinnedNext").focus();
  } catch (error) {
    if (nextRequest === controller) setText("pinnedRequestStatus", error.message);
  } finally {
    if (nextRequest === controller) {
      nextRequest = undefined;
      byId("findPinnedNext").disabled = byId("barFindNext").disabled = false;
    }
  }
}

byId("pinRoute").addEventListener("click", (event) => requestPin(true, event.currentTarget));
byId("pinNight").addEventListener("click", (event) => requestPin(false, event.currentTarget));
byId("openPinnedTrip").addEventListener("click", (event) => openPinnedTimeline(event.currentTarget));
byId("closePinnedTrip").addEventListener("click", () => pinnedDialog.close());
byId("cancelPinnedReplace").addEventListener("click", () => replaceDialog.close());
byId("confirmPinnedReplace").addEventListener("click", () => {
  if (replacement) setPinned(replacement);
  replaceDialog.close();
});
replaceDialog.addEventListener("close", () => { replacement = undefined; });
byId("unpinTrip").addEventListener("click", () => unpin(true));
byId("unpinFromTimeline").addEventListener("click", () => unpin(true));
byId("undoUnpin").addEventListener("click", () => {
  const previous = undoTrip;
  undoTrip = null;
  byId("tripUndo").hidden = true;
  if (previous && Date.now() < previous.until && !pinnedExpired(previous.trip)) {
    setPinned(previous.trip);
    byId("openPinnedTrip").focus();
  }
});
byId("donePinnedStep").addEventListener("click", () => {
  if (!pinnedTrip || activeStep < 0) return;
  if (activeStep >= pinnedSteps.length - 1) {
    unpin();
    return;
  }
  pinnedTrip.through = activeStep;
  savePinned();
  updatePinnedTrip();
});
byId("showPinnedMap").addEventListener("click", () => {
  const bounds = L.latLngBounds([coordinates(pinnedTrip.place)]);
  for (const itinerary of [pinnedTrip.outbound, pinnedTrip.home].filter(Boolean)) {
    for (const leg of itinerary.legs) bounds.extend(leg.geometry);
  }
  pinnedDialog.close();
  map.fitBounds(bounds, { maxZoom: 16, padding: [24, 24], animate: !reducedMotion });
});
byId("barFindNext").addEventListener("click", (event) => findPinnedNext(event.currentTarget));
byId("findPinnedNext").addEventListener("click", (event) => findPinnedNext(event.currentTarget));
byId("confirmPinnedNext").addEventListener("click", () => {
  if (!nextRoute) return;
  const itinerary = pinnedSteps[activeStep]?.section === "HOME" ? nextRoute.home : nextRoute.outbound;
  if (!itinerary || !catchable(itinerary, pinnedNow(pinnedTrip))) {
    clearNextRoute();
    setText("pinnedRequestStatus", "That route has passed. Find the next one.");
    byId("findPinnedNext").focus();
    return;
  }
  setPinned(nextRoute);
  byId("donePinnedStep").focus();
});
pinnedDialog.addEventListener("close", () => {
  const trigger = dialogTriggers.get(pinnedDialog);
  (trigger && !trigger.hidden && !trigger.closest("[hidden]") ? trigger : pinnedTrip ? byId("openPinnedTrip") :
    undoTrip ? byId("undoUnpin") : byId("planTab")).focus();
});
replaceDialog.addEventListener("close", () => {
  const trigger = dialogTriggers.get(replaceDialog);
  (trigger && !trigger.hidden ? trigger : byId("openPinnedTrip")).focus();
});
document.addEventListener("night-plan-changed", updatePinControls);
setInterval(() => { updatePinnedTrip(); updatePinControls(); }, 1000);
if (pinnedTrip) setPinned(pinnedTrip);
