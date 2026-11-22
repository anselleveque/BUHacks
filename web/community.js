"use strict";

const profileKey = "midnight-munch-profile";
const avatarColors = {
  red: "#e9918a", yellow: "#f7db7e", green: "#acd3a0", blue: "#a4c9ec",
  purple: "#cab5e8", pink: "#f3c0d3", white: "#fff",
  midnight: "#34486d", moss: "#365b4c", plum: "#654669"
};
const avatarGroups = {
  Faces: [["😀","Smile"],["😊","Happy face"],["😎","Sunglasses"],["🤠","Cowboy"],["👧","Girl"],["👦","Boy"]],
  Animals: [["🐶","Dog"],["🐱","Cat"],["🦊","Fox"],["🐼","Panda"],["🐸","Frog"],["🐻","Bear"]],
  Food: [["🍕","Pizza"],["🍜","Noodles"],["🍔","Burger"],["🌮","Taco"],["🍓","Strawberry"],["🍩","Doughnut"]],
  Night: [["🌙","Moon"],["⭐","Star"],["🦉","Owl"],["🦇","Bat"],["🌃","Night skyline"],["✨","Sparkles"]]
};
const avatarChoices = Object.values(avatarGroups).flat().map(([emoji]) => emoji);
const freshProfile = () => ({ name: "", avatar: "🐶", color: "blue", bio: "", defaults: {} });
let profile = freshProfile();
try {
  const current = localStorage.getItem(profileKey);
  const legacy = current === null ? localStorage.getItem("bite-boston-profile") : null;
  const saved = JSON.parse(current ?? legacy);
  if (saved) profile = {
    name: typeof saved.name === "string" ? saved.name.slice(0, 28) : "",
    bio: typeof saved.bio === "string" ? saved.bio.slice(0, 140) : "",
    avatar: avatarChoices.includes(saved.avatar) ? saved.avatar : profile.avatar,
    color: Object.hasOwn(avatarColors, saved.color) ? saved.color : profile.color,
    defaults: saved.defaults && typeof saved.defaults === "object" && !Array.isArray(saved.defaults) ? saved.defaults : {}
  };
  if (legacy !== null) localStorage.setItem(profileKey, JSON.stringify(profile));
} catch {
  // The profile starts with defaults if storage is unavailable.
}

const profileStats = { visits: "Visits", stamps: "Stamps", food_logs: "Food logs", night_visits: "Night visits",
  photos: "Photos", price_reports: "Price reports", item_reports: "Items reported", dishes_added: "Dishes added",
  areas_visited: "Areas visited", first_visit_at: "First visit" };
const levels = [[0,"Newcomer"],[50,"Regular"],[150,"Night Owl"],[400,"Local Legend"],[1000,"Midnight Muncher"]];
const emptyAccount = () => ({ visits: [], stamps: [], points: {}, food_logs: [], stats: {}, badges: [], favorites: [], history: [] });
let account = emptyAccount();
let accountVersion = 0;
const communityPrices = new Map();
let priceVersion = 0;
const knownMenus = new Map();
const visitPending = new Set();
const visitMessages = new Map();
const demoCheckIn = new URLSearchParams(location.search).get("demo") === "1";

const visited = (place) => account.visits.some(({ place_id }) => place_id === place.id) || place.visited === true;
const stamped = (place) => account.stamps.some(({ place_id }) => place_id === place.id) || place.stamped === true;

function stampIcon(place) {
  const earned = stamped(place);
  const node = document.createElement("span");
  node.append(makeIcon(earned ? "check" : "plus"));
  node.className = earned ? "stamp earned" : "stamp";
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", earned ? "Stamp earned" : "Stamp available");
  return node;
}

function paintAvatar(node, avatar = profile.avatar, color = profile.color) {
  node.textContent = avatar;
  node.style.backgroundColor = avatarColors[color];
}

function renderProfile() {
  paintAvatar(byId("profileAvatar"));
  setText("profileName", profile.name || "My profile");
  const points = account.points.total ?? 0;
  const fallbackLevel = levels.findLastIndex(([threshold]) => threshold <= points);
  const level = account.level ?? { name: levels[fallbackLevel][1], points, next_at: levels[fallbackLevel + 1]?.[0] };
  setText("profileLevel", level.name);
  setText("profileScore", `${level.points} pts`);
  setText("levelSummary", `${level.name} · ${level.points} pts`);
  const next = levels.find(([threshold]) => threshold === level.next_at);
  const floor = levels.filter(([threshold]) => threshold <= level.points).at(-1)?.[0] ?? 0;
  const progress = byId("levelProgress");
  progress.hidden = !next;
  if (next) {
    progress.max = Math.max(1, next[0] - floor);
    progress.value = Math.max(0, level.points - floor);
  }
  setText("levelNext", next ? `${level.to_next ?? next[0] - level.points} pts to ${next[1]}` : "Top level reached");
  setText("profileStreak", `${account.stats?.streak_days ?? 0} day streak`);
  const badges = byId("profileBadges");
  badges.replaceChildren();
  for (const badge of account.badges ?? []) {
    const item = element("li", "", badges);
    item.className = badge.earned ? "badge-earned" : "badge-locked";
    element("strong", badge.name, item);
    element("small", badge.earned ? "Earned" : `Locked · ${badge.progress} / ${badge.goal}`, item);
  }
  if (!badges.childElementCount) element("li", "Badges will appear here as you explore.", badges);
  const list = byId("stampList");
  list.replaceChildren();
  const dates = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "America/New_York" });
  for (const stamp of account.stamps) {
    const row = element("li", "", list);
    row.append(stampIcon({ stamped: true }));
    element("strong", stamp.name, row);
    const date = new Date(stamp.earned_at);
    if (Number.isFinite(date.getTime())) {
      const time = element("time", dates.format(date), row);
      time.dateTime = stamp.earned_at;
    }
  }
  if (!account.stamps.length) element("li", "Log a restaurant and save food to earn a stamp.", list);
  const stats = byId("profileStats");
  stats.replaceChildren();
  for (const [key, label] of Object.entries(profileStats)) {
    element("dt", label, stats);
    const value = account.stats?.[key];
    element("dd", key === "first_visit_at" ? profileDate(value) || "No visits yet" : value ?? 0, stats);
  }
  const logs = byId("profileFoodLogs");
  logs.replaceChildren();
  for (const log of account.food_logs ?? []) {
    const row = element("li", "", logs);
    element("strong", log.name, row);
    const date = new Date(log.created_at);
    if (Number.isFinite(date.getTime())) element("time", dates.format(date), row).dateTime = log.created_at;
    const items = element("ul", "", row);
    for (const item of log.items) {
      const price = Number.isInteger(item.price_cents) ? ` · ${money.format(item.price_cents / 100)}` : "";
      element("li", item.name + price, items);
    }
  }
  if (!logs.childElementCount) element("li", "No food logged yet.", logs);
  renderProfileHistory();
  renderProfileFavorites();
}

async function refreshAccount() {
  const version = ++accountVersion;
  try {
    const data = await get("/api/me");
    if (version !== accountVersion) return;
    account = { ...emptyAccount(), ...data };
    setText("accountStatus", "");
    document.dispatchEvent(new Event("account-refreshed"));
    renderProfile();
    renderPlaces();
    renderSelectedMarker();
    renderSelectedDetails();
  } catch (error) {
    if (version === accountVersion) setText("accountStatus", error.message);
  }
}

const dialogTriggers = new WeakMap();
function openDialog(dialog, trigger) {
  dialogTriggers.set(dialog, trigger);
  dialog.showModal();
}
for (const dialog of document.querySelectorAll("dialog:has([data-close])")) {
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (dialog.open) return;
    const trigger = dialogTriggers.get(dialog);
    if (trigger?.isConnected && !trigger.disabled && !trigger.closest("[hidden]")) trigger.focus();
    else if (dialog.id === "priceDialog" && !byId("selectedDetails").hidden) byId("nightHeading").focus();
    else byId("profileOpen").focus();
  });
}

const profileDialog = byId("profileDialog");
for (const [name, avatars] of Object.entries(avatarGroups)) {
  const group = element("fieldset", "", byId("avatarGrid"));
  element("legend", name, group);
  const grid = element("div", "", group);
  grid.className = "profile-swatches";
  for (const [emoji, label] of avatars) {
    addChip(grid, "profile-avatar", emoji, emoji, "radio").setAttribute("aria-label", label);
  }
}
for (const [name, color] of Object.entries(avatarColors)) {
  const input = addChip(byId("colorGrid"), "profile-color", name, "", "radio");
  input.setAttribute("aria-label", name);
  input.nextElementSibling.style.backgroundColor = color;
}
const identityValue = (key) => byId("profileForm").querySelector(`input[name="profile-${key}"]:checked`)?.value;
function previewAvatar() {
  paintAvatar(byId("avatarPreview"), identityValue("avatar"), identityValue("color"));
}
function fillIdentity() {
  byId("displayName").value = profile.name;
  byId("bio").value = profile.bio;
  for (const key of ["avatar", "color"]) {
    for (const input of byId("profileForm").querySelectorAll(`input[name="profile-${key}"]`)) input.checked = input.value === profile[key];
  }
  previewAvatar();
}
function saveProfile(status) {
  try {
    localStorage.setItem(profileKey, JSON.stringify(profile));
    setText(status, "Saved.");
  } catch {
    setText(status, "This browser could not save your profile.");
  }
  // profile-sync.js saves the change on the server too (debounced).
  document.dispatchEvent(new CustomEvent("profile-changed", { detail: { status } }));
}
byId("profileOpen").addEventListener("click", (event) => {
  fillIdentity();
  renderDefaults();
  setText("profileStatus", "");
  openDialog(profileDialog, event.currentTarget);
  profileDialog.scrollTop = 0;
  refreshAccount();
});
byId("profileForm").addEventListener("change", previewAvatar);
byId("profileForm").addEventListener("submit", (event) => {
  event.preventDefault();
  Object.assign(profile, { name: byId("displayName").value.trim(), avatar: identityValue("avatar"),
    color: identityValue("color"), bio: byId("bio").value.trim() });
  saveProfile("profileStatus");
  renderProfile();
  renderOrigin();
});

const defaultFields = {
  station: "Home station", budget: "Budget", stay: "Stay", walk: "Walk limit", travel: "Time to get there",
  areas: "Areas", late: "Late-night default", rideHome: "Count the ride home", diet: "Dietary needs", craving: "Cravings"
};
const defaultChoices = (selector) => [...document.querySelectorAll(selector)].filter((input) => input.value)
  .map((input) => [input.value, input.nextElementSibling.textContent]);
function profileChoices(key) {
  switch (key) {
    case "station": return [...station.options].filter((option) => option.value).map((option) => [option.value, option.text]);
    case "budget": return Array.from({ length: Math.floor((Number(budget.max) - Number(budget.min)) / Number(budget.step)) + 1 },
      (_, i) => { const value = Number(budget.min) + i * Number(budget.step); return [value, `$${value}`]; });
    case "stay": return defaultChoices('#preferences [name="stay"]');
    case "walk": return defaultChoices('#walkChoices input');
    case "travel": return [["any","Any"], ...travelLimits.map((value) => [value, `${value} min`])];
    case "areas": return areaOptions.map((area) => [area, area]);
    case "late": return [["auto","Automatic by clock"],["on","Late night"],["off","Anytime"]];
    case "rideHome": return [["yes","Yes"],["no","No"]];
    case "diet": return defaultChoices('#diets input');
    case "craving": return defaultChoices('#cravings input');
  }
}

function applyDefault(key, reset = false) {
  const saved = profile.defaults[key];
  if (saved == null && !reset) return;
  const value = String(saved ?? "");
  if (key === "station") {
    if (stations.has(value) || reset) {
      station.value = value;
      station.dispatchEvent(new Event("change"));
    }
    return;
  }
  const selectChip = (container, chosen) => {
    for (const input of byId(container).querySelectorAll("input")) input.checked = input.value === String(chosen);
  };
  switch (key) {
    case "budget": budget.value = reset ? "12" : value; budgetLabel(); break;
    case "stay": if (reset || [30,45,60,90].includes(Number(value))) setStay(reset ? 45 : Number(value)); break;
    case "walk":
      if (reset || profileChoices("walk").some(([option]) => option === value)) {
        walkOk = reset ? plannerWalkDefault : Number(value);
        selectChip("walkChoices", walkOk);
        budgetLabel();
      }
      break;
    case "travel": setTravelLimit(travelLimits.includes(Number(value)) ? Number(value) : undefined); break;
    case "areas":
    case "diet":
    case "craving": {
      const values = Array.isArray(saved) ? saved.slice(0, 12) : [];
      const container = { areas: "areaChoices", diet: "diets", craving: "cravings" }[key];
      for (const input of byId(container).querySelectorAll("input")) input.checked = values.includes(input.value);
      if (key === "areas" && byId(container).firstChild) updateAreaSummary();
      break;
    }
    case "late": {
      const hour = Number(new Intl.DateTimeFormat("en-US", { hour: "numeric", hourCycle: "h23", timeZone: "America/New_York" }).format(planningNow()));
      setLate(value === "on" || value !== "off" && (hour >= 21 || hour < 5));
      break;
    }
    case "rideHome":
      byId("countRideHome").checked = reset ? plannerFareDefault : value !== "no";
      budgetLabel();
      break;
  }
  updateFilterSummary();
  schedulePlaces();
  scheduleLateStrip();
}

function renderDefaults() {
  const container = byId("profileDefaults");
  container.replaceChildren();
  for (const [key, title] of Object.entries(defaultFields)) {
    const row = element("fieldset", "", container);
    element("legend", title, row);
    const choices = profileChoices(key);
    if (["areas", "diet", "craving"].includes(key)) {
      const chips = element("div", "", row);
      chips.className = "chips";
      for (const [value, text] of choices) {
        const input = addChip(chips, `default-${key}`, value, text);
        input.checked = Array.isArray(profile.defaults[key]) && profile.defaults[key].includes(value);
      }
      if (!choices.length) element("p", "Options are unavailable right now.", row);
      chips.addEventListener("change", () => {
        const values = checkedValues(chips);
        if (key === "areas" && values.length > 12) {
          const input = [...chips.querySelectorAll("input:checked")].at(-1);
          input.checked = false;
        }
        profile.defaults[key] = checkedValues(chips);
        applyDefault(key);
        saveProfile("defaultsStatus");
      });
    } else {
      const select = element("select", "", row);
      select.id = `default-${key}`;
      select.setAttribute("aria-label", title);
      select.add(new Option("App default", ""));
      for (const [value, text] of choices) select.add(new Option(text, value));
      select.value = String(profile.defaults[key] ?? "");
      if (select.selectedIndex < 0) select.selectedIndex = 0;
      select.addEventListener("change", () => {
        if (select.value) profile.defaults[key] = select.value;
        else {
          delete profile.defaults[key];
          if (key === "walk" || key === "rideHome") removePlannerPreference(key);
        }
        applyDefault(key, !select.value);
        saveProfile("defaultsStatus");
        if (key === "station") useStart.disabled = !select.value;
      });
    }
    const reset = element("button", "Reset", row);
    reset.type = "button";
    reset.className = "text-action";
    reset.setAttribute("aria-label", `Reset ${title.toLowerCase()}`);
    reset.addEventListener("click", () => {
      delete profile.defaults[key];
      if (key === "walk" || key === "rideHome") removePlannerPreference(key);
      applyDefault(key, true);
      saveProfile("defaultsStatus");
      for (const input of row.querySelectorAll("input")) input.checked = false;
      const select = row.querySelector("select");
      if (select) select.selectedIndex = 0;
      if (key === "station") useStart.disabled = true;
    });
    let useStart;
    if (key === "station") {
      useStart = element("button", "Use as start", row);
      useStart.type = "button";
      useStart.disabled = !stations.has(profile.defaults.station);
      useStart.addEventListener("click", () => {
        const home = stations.get(profile.defaults.station);
        if (home) chooseStation(home);
        profileDialog.close();
      });
    }
  }
}

function removePlannerPreference(key) {
  try { localStorage.removeItem(key === "walk" ? "midnight-munch-walk-ok" : "midnight-munch-fare-trips"); } catch {}
}
document.addEventListener("planner-options-ready", () => {
  for (const key of Object.keys(defaultFields).filter((key) => key !== "station")) applyDefault(key);
  if (profileDialog.open) renderDefaults();
});
document.addEventListener("planner-stations-ready", () => {
  if (!locationPoint) applyDefault("station");
  if (profileDialog.open) renderDefaults();
});
applyDefault("late");

const historyKey = "midnight-munch-pinned-history";
const profileDates = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "America/New_York" });
function profileDate(value) {
  const date = new Date(value ?? NaN);
  return Number.isFinite(date.getTime()) ? profileDates.format(date) : "";
}
function localNights() {
  try {
    const list = JSON.parse(localStorage.getItem(historyKey));
    return Array.isArray(list) ? list.filter((item) => Number.isInteger(item?.place_id) &&
      typeof item.name === "string" && typeof item.mode === "string" && profileDate(item.visited_at)).slice(0, 20) : [];
  } catch { return []; }
}
function recordPinnedNight(trip) {
  const route = [trip.outbound, trip.home].filter(Boolean);
  const mode = [...new Set(route.map((option) => walkOnly(option) ? "Walk" : rideLegs(option).map(rideLabel).join(" → ")))].join(" / ");
  const visited_at = trip.home?.summary.arrive_iso ?? trip.outbound.summary.arrive_iso;
  const night = { place_id: trip.place.id, name: trip.place.name, visited_at, mode };
  const existing = localNights().filter((item) => item.place_id !== night.place_id || item.visited_at !== visited_at);
  try { localStorage.setItem(historyKey, JSON.stringify([night, ...existing].slice(0, 20))); } catch {}
  if (profileDialog.open) renderProfileHistory();
}

function openProfilePlace(item, button) {
  if (button.disabled) return;
  button.disabled = true;
  get("/api/places", withDemoTime({ q: item.name, menus: "all", browse: 1 })).then(({ places }) => {
    if (!profileDialog.open) return;
    const place = places.find(({ id }) => id === item.place_id) ?? { id: item.place_id, name: item.name };
    profileDialog.addEventListener("close", () => byId("nightHeading").focus({ preventScroll: true }), { once: true });
    profileDialog.close();
    planPlace(place);
  }).catch((error) => setText("accountStatus", error.message)).finally(() => { button.disabled = false; });
}

function renderProfileHistory() {
  const list = byId("profileHistory");
  list.replaceChildren();
  const nights = [...(account.history ?? []), ...localNights()].sort((a, b) => Date.parse(b.visited_at) - Date.parse(a.visited_at));
  for (const night of nights) {
    const row = element("li", "", list);
    const button = element("button", "", row);
    button.type = "button";
    button.addEventListener("click", () => openProfilePlace(night, button));
    element("strong", night.name, button);
    element("small", [profileDate(night.visited_at), night.mode].filter(Boolean).join(" · "), button);
    if (typeof night.photo_url === "string" && /^\/api\/visit-photo\/[\w-]+$/.test(night.photo_url)) {
      const photo = element("img", "", button);
      photo.alt = "Photo from this night";
      photo.loading = "lazy";
      photo.src = night.photo_url;
    }
    for (const item of night.items ?? []) {
      const price = Number.isInteger(item.price_cents) ? ` · ${money.format(item.price_cents / 100)}` : "";
      element("small", `${typeof item === "string" ? item : item.name}${price}`, button);
    }
  }
  if (!nights.length) element("li", "Log a restaurant or finish a pinned trip to see it here.", list);
}

const favoritePending = new Set();
const favoriteOverrides = new Map();
const isFavorite = (place) => favoriteOverrides.get(place.id) ??
  (account.favorites.some(({ place_id }) => place_id === place.id) || place.favorite === true);
function paintFavorite(button, place) {
  const saved = isFavorite(place);
  button.textContent = saved ? "♥" : "♡";
  button.setAttribute("aria-pressed", String(saved));
  button.setAttribute("aria-label", `${saved ? "Remove favorite" : "Favorite"}: ${place.name}`);
  button.disabled = favoritePending.has(place.id);
  button.onclick = () => toggleFavorite(place, button);
}
function addFavorite(place, parent) {
  const button = element("button", "", parent);
  button.type = "button";
  button.className = "favorite-heart";
  button.dataset.favoriteId = place.id;
  paintFavorite(button, place);
}
function renderProfileFavorites() {
  const list = byId("profileFavorites");
  list.replaceChildren();
  for (const item of account.favorites) {
    const row = element("li", "", list);
    const button = element("button", item.name, row);
    button.type = "button";
    button.addEventListener("click", () => openProfilePlace(item, button));
    addFavorite({ id: item.place_id, name: item.name }, row);
  }
  if (!account.favorites.length) element("li", "Tap a heart beside a place to save it here.", list);
}
async function toggleFavorite(place, button) {
  if (favoritePending.has(place.id)) return;
  const favorite = !isFavorite(place);
  const hadFocus = document.activeElement === button;
  favoritePending.add(place.id);
  button.disabled = true;
  try {
    await postCommunity("/api/favorite", { place_id: place.id, favorite });
    favoriteOverrides.set(place.id, favorite);
    account.favorites = account.favorites.filter((item) => item.place_id !== place.id);
    if (favorite) account.favorites.unshift({ place_id: place.id, name: place.name });
    renderProfileFavorites();
    setText(profileDialog.open ? "accountStatus" : "favoriteStatus", favorite ? "Added to favorites." : "Removed from favorites.");
  } catch (error) {
    setText(profileDialog.open ? "accountStatus" : "favoriteStatus", error.message);
  } finally {
    favoritePending.delete(place.id);
    if (button.isConnected) paintFavorite(button, place);
    renderPlaces();
    if (pickedPlace) paintFavorite(byId("heroFavorite"), pickedPlace);
    if (!hadFocus || document.activeElement !== document.body && document.activeElement !== button) return;
    if (profileDialog.open && !button.isConnected) {
      (byId("profileFavorites").querySelector(".favorite-heart") ?? byId("profileFavorites").previousElementSibling).focus();
    } else if (!profileDialog.open && !button.isConnected) {
      (byId("placeList").querySelector(`[data-favorite-id="${place.id}"]`) ?? byId("favoritesOnly")).focus();
    } else if (button.isConnected) button.focus();
  }
}

byId("exportProfile").addEventListener("click", async () => {
  try {
    const data = await get("/api/me/export");
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const download = document.createElement("a");
    download.href = url;
    download.download = "midnight-munch-data.json";
    download.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    setText("privacyStatus", "Your data download is ready.");
  } catch (error) { setText("privacyStatus", error.message); }
});
byId("deleteProfileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("deleteConfirm");
  const button = event.submitter;
  if (input.value !== "delete") {
    setText("privacyStatus", "Type delete to confirm.");
    input.focus();
    return;
  }
  button.disabled = true;
  try {
    await postCommunity("/api/me/delete", { confirm: input.value });
    accountVersion++;
    account = emptyAccount();
    favoriteOverrides.clear();
    visitMessages.clear();
    for (const place of [...nearbyPlaces, pickedPlace].filter(Boolean)) {
      place.favorite = place.visited = place.stamped = false;
    }
    renderProfile();
    renderPlaces();
    renderSelectedMarker();
    renderSelectedDetails();
    schedulePlaces();
    input.value = "";
    byId("deleteDisclosure").open = false;
    byId("deleteDisclosure").querySelector("summary").focus();
    setText("privacyStatus", "Your data on this server was deleted.");
    document.dispatchEvent(new Event("server-data-deleted"));
  } catch (error) { setText("privacyStatus", error.message); }
  finally { button.disabled = false; }
});
byId("resetBrowserProfile").addEventListener("click", () => {
  try {
    for (const key of [profileKey, "bite-boston-profile", historyKey, recentStationsKey]) localStorage.removeItem(key);
  } catch {
    setText("privacyStatus", "This browser could not reset your profile.");
    return;
  }
  profile = freshProfile();
  removePlannerPreference("walk");
  removePlannerPreference("rideHome");
  for (const key of Object.keys(defaultFields)) applyDefault(key, true);
  fillIdentity();
  renderDefaults();
  renderProfile();
  renderOrigin();
  setText("privacyStatus", "This browser’s profile was reset.");
  document.dispatchEvent(new Event("browser-profile-reset"));
});

function renderSelectedDetails() {
  const place = pickedPlace;
  if (!place) return;
  paintFavorite(byId("heroFavorite"), place);
  const heading = byId("selectedName");
  heading.textContent = place.name + " ";
  heading.append(stampIcon(place));
  const details = byId("selectedInfo");
  details.replaceChildren();
  const fromPrice = nearbyPlaces.find(({ id }) => id === place.id)?.from_price ?? place.from_price;
  if (place.has_menu === false) {
    showPriceTier(place, details);
    element("p", "No public menu", details);
  } else if (!currentDish && Number.isFinite(fromPrice)) element("p", `From ${money.format(fromPrice)}`, details);
  if (!currentDish) {
    if (place.area) element("p", place.area, details);
    if (place.stop_line) element("p", place.stop_line, details);
    element("p", place.hours_text || "Hours unavailable", details);
  }
  const button = byId("logVisit");
  const logged = visited(place);
  const pending = visitPending.has(place.id);
  button.disabled = logged || pending || !hasCoordinates(place);
  button.textContent = logged ? "✓ Visit logged" : pending ? "Checking location…" : "Log restaurant";
  setText("logStatus", logged
    ? stamped(place) ? "Stamp added to your profile." : "Save a food name to earn your stamp."
    : visitMessages.get(place.id) || (hasCoordinates(place) ? "" : "Location coordinates are unavailable for this restaurant."));
}

async function postCommunity(path, data) {
  const multipart = data instanceof FormData;
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: multipart ? {} : { "Content-Type": "application/json" },
    body: multipart ? data : JSON.stringify(data),
    signal: AbortSignal.timeout(30000)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.error) {
    const error = new Error(result.error || "The request couldn’t be saved. Try again.");
    error.suggest = result.suggest;
    throw error;
  }
  return result;
}

function gpsFix() {
  // Developer controls can answer instead of the device (Round 32); null means "use the device".
  const simulated = window.midnightMunchDev?.position();
  if (simulated) return simulated;
  return new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(resolve, reject, {
    enableHighAccuracy: true, timeout: 10000, maximumAge: 0
  }));
}

byId("logVisit").addEventListener("click", async () => {
  const place = pickedPlace;
  if (!place || visited(place) || visitPending.has(place.id)) return;
  visitMessages.delete(place.id);
  visitPending.add(place.id);
  const hadFocus = document.activeElement === byId("logVisit");
  renderSelectedDetails();
  try {
    const data = { place_id: place.id };
    if (demoCheckIn) {
      data.demo = true;
    } else {
      if (!devGps.enabled && (!navigator.geolocation || !window.isSecureContext)) {
        throw new Error("Location needs HTTPS or localhost to check in.");
      }
      const { coords } = await gpsFix().catch(({ code }) => {
        throw new Error(code === 1 ? "Allow location in your browser, then try again." : "Couldn’t get a GPS fix. Try again.");
      });
      const { latitude: lat, longitude: lng, accuracy: accuracy_m } = coords;
      if (accuracy_m > 60.96) throw new Error("GPS is too imprecise. Try again with a clearer view of the sky.");
      const meters = map.distance([lat, lng], coordinates(place));
      if (meters > 60.96) {
        throw new Error(`GPS puts you about ${Math.round(meters / .3048)} ft away. Get within 200 ft to log this restaurant.`);
      }
      Object.assign(data, { lat, lng, accuracy_m });
    }
    const result = await postCommunity("/api/visit", data);
    accountVersion++;
    account.visits.push({ place_id: place.id, name: place.name, visited_at: result.visited_at });
    account.points = result.points;
    renderProfile();
    refreshAccount();
    document.dispatchEvent(new Event("community-checkin"));
  } catch (error) {
    visitMessages.set(place.id, error.message);
  } finally {
    visitPending.delete(place.id);
    renderSelectedDetails();
    if (hadFocus && pickedPlace?.id === place.id && document.activeElement === document.body) {
      (visited(place) ? byId("logFood") : byId("logVisit")).focus();
    }
  }
});

function rememberDish(dish) {
  if (!knownMenus.has(dish.place.id)) knownMenus.set(dish.place.id, new Map());
  knownMenus.get(dish.place.id).set(dish.item_id, {
    item_id: dish.item_id, name: dish.name, price: dish.price,
    price_original: dish.price_original, price_status: dish.price_status,
    community: dish.community, votes: dish.votes, aliases: dish.aliases
  });
}

function communityPriceNote(item) {
  return item?.price_status === "reported" && Number.isFinite(item.price_original)
    ? `community price (menu ${money.format(item.price_original)})` : "";
}

function displayedFare(dish, outbound = selectedTrip, home = selectedHomeTrip,
    { trips = tripFares.trips ?? dish.fare_trips ?? fareTrips(), each = tripFares.each, walkHome = homeModeOverride } = {}) {
  const go = rideLegs(outbound);
  const back = rideLegs(home);
  const verified = [...(dish.transit?.vehicles ?? []), ...(dish.transit?.home_vehicles ?? [])];
  const vehicles = outbound
    ? [...go, ...back].map((leg) => leg.vehicle ?? "subway") : verified;
  const busOnly = vehicles.length > 0 && vehicles.every((vehicle) => vehicle === "bus");
  const apiFare = (route) => route && (route.fare_each ?? route.fare ?? each);
  const goFare = outbound && walkOnly(outbound) ? 0 : apiFare(outbound) ?? dish.fare_each;
  const previousReturn = trips === 2 && Number.isFinite(dish.fare) && Number.isFinite(dish.fare_each) && dish.fare > 0
    ? Math.round((dish.fare - dish.fare_each) * 100) / 100 : goFare;
  const returnEach = home ? walkOnly(home) ? 0 : apiFare(home) ?? previousReturn
    : (walkHome ?? (outbound && walkOnly(outbound))) ? 0 : previousReturn;
  const total = Number.isFinite(goFare) && (trips === 1 || Number.isFinite(returnEach))
    ? goFare + (trips === 2 ? returnEach : 0) : dish.fare;
  const perRide = goFare || returnEach;
  const busKnown = Number.isFinite(perRide) && (Number.isFinite(apiFare(outbound)) ||
    Number.isFinite(apiFare(home)) || verified.length > 0 && verified.every((vehicle) => vehicle === "bus"));
  return { busOnly, busKnown, each: perRide, goFare, returnEach, total, trips, chargedTrips: Number(goFare > 0) + Number(trips === 2 && returnEach > 0) };
}

function renderDishPrice(dish, startingPrice = /^sizes:/i.test(dish.price_note?.trim() ?? "") ||
    dish.notes?.trim().toLowerCase() === "from") {
  const cents = communityPrices.get(String(dish.place.id))?.[dish.item_id];
  // A finite picker price is already effective for this specific branch.
  const usesCachedPrice = !Number.isFinite(dish.price) && Number.isInteger(cents);
  const price = usesCachedPrice ? cents / 100 : dish.price;
  setText("price", `${startingPrice ? "from " : ""}${money.format(price)}`);
  const showFare = locationPoint && locationPoint === dishFareStart && dish.fare_included === true;
  const fare = displayedFare(dish);
  const heroTransport = byId("heroTransport");
  heroTransport.replaceChildren();
  const seen = new Set();
  for (const leg of rideLegs(selectedTrip)) {
    const label = rideLabel(leg);
    if (seen.has(label)) continue;
    seen.add(label);
    rideChip(leg, heroTransport);
  }
  const breakdown = byId("fareBreakdown");
  breakdown.hidden = !showFare || !Number.isFinite(fare.total);
  // Once a route is chosen, its own duration replaces the picker's estimate.
  if (selectedTrip?.summary && Number.isFinite(selectedTrip.summary.duration_min)) {
    setText("dishTravel", walkOnly(selectedTrip) ? `${selectedTrip.summary.duration_min} min walk there`
      : `${selectedTrip.summary.duration_min} min there by ${routeChipVehicles(selectedTrip)}`);
  }
  // The hero and this breakdown use the same two numbers: the chosen route out and the chosen way home.
  const leg = (value) => value > 0 ? money.format(value) : "free";
  if (!breakdown.hidden) {
    const parts = [];
    if (Number.isFinite(fare.goFare)) parts.push(`out ${leg(fare.goFare)}`);
    if (fare.trips === 2 && Number.isFinite(fare.returnEach)) parts.push(`home ${leg(fare.returnEach)}`);
    breakdown.textContent = parts.length ? `Fares: ${parts.join(" · ")}` : "";
  }
  const total = Math.round(100 * (price + (showFare && Number.isFinite(fare.total) ? fare.total : 0))) / 100;
  setText("dishTotal", money.format(total));
  setText("shortFare", breakdown.hidden ? "" : !fare.total ? "No fares, walking" :
    `${fare.chargedTrips === 2 ? "Fares" : "Fare"} ${money.format(fare.total)}`);
  byId("overBudget").hidden = total <= Number(budget.value);
  byId("resultBudget").hidden = total > Number(budget.value);
  setText("overBudget", `Over your budget by ${money.format(total - Number(budget.value))}`);
  setText("resultBudget", `Budget ${money.format(Number(budget.value))} · ${money.format(Math.max(0, Number(budget.value) - total))} left`);
  setText("communityPrice", dish.community === true ? "price reported by diners" : communityPriceNote(usesCachedPrice ? { ...dish, price_status: "reported" } : dish));
}

function applyCommunityPrices(response, placeId) {
  priceVersion++;
  for (const [id, prices] of Object.entries(response.prices_by_place ?? {})) {
    communityPrices.set(id, prices);
  }
  if (placeId != null && response.prices) {
    communityPrices.set(String(placeId), response.prices_by_place?.[placeId] ?? response.prices);
  } else if (response.place_id != null && response.prices) {
    communityPrices.set(String(response.place_id), response.prices);
  }
  if (currentDish) renderDishPrice(currentDish);
  schedulePlaces();
}

const foodDialog = byId("foodDialog");
const priceDialog = byId("priceDialog");
const foodForm = byId("foodForm");
const foodItems = byId("foodItems");
const priceForm = byId("priceForm");
const photoInput = byId("foodPhoto");
let photoUrl;
let feedback;
let foodPending = false;
let pricePending = false;

function communitySubmitPending() {
  return foodPending || pricePending;
}

const isOfficialDish = (item) => Number.isInteger(item?.item_id) && item.item_id > 0 && !item.community;
const menuValue = (item) => item.item_id === null ? `community:${item.name}` : String(item.item_id);
const refreshSuggestions = new WeakMap();

function nameTokens(name) {
  return name.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
}

function matchesMenuName(query, name) {
  const words = nameTokens(name);
  return query.every((part) => words.some((word) => word.startsWith(part)));
}

function chooseMenuItem(select, item) {
  const value = menuValue(item);
  if (![...select.options].some((option) => option.value === value)) {
    if (!isOfficialDish(item)) return;
    if (!feedback.menu.some((entry) => entry.item_id === item.item_id)) feedback.menu.push(item);
    select.add(new Option(item.name, value));
  }
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  select.focus();
}

function showOfficialSuggestion(submittedRows, items, suggestion) {
  if (!suggestion || !isOfficialDish(suggestion) || typeof suggestion.name !== "string") return;
  const tokens = nameTokens(suggestion.name);
  const otherRows = submittedRows.filter((row, index) => items[index].other &&
    row.isConnected && row.querySelector("select").value === "other" &&
    row.querySelector("input").value.trim() === items[index].other);
  if (!otherRows.length) return;
  const matchScore = (row) => nameTokens(row.querySelector("input").value)
    .filter((word) => tokens.some((token) => token.startsWith(word) || word.startsWith(token))).length;
  const best = Math.max(...otherRows.map(matchScore));
  const matches = otherRows.filter((row) => matchScore(row) === best);
  for (const row of matches) {
    const region = row.querySelector(".official-suggestion");
    const button = element("button", `Did you mean ${suggestion.name}?`, region);
    button.type = "button";
    button.addEventListener("click", () => {
      chooseMenuItem(row.querySelector("select"), suggestion);
      for (const otherRow of matches) otherRow.querySelector(".official-suggestion").replaceChildren();
      setText("foodStatus", "");
    });
    if (row === matches[0]) button.focus();
  }
}

function menuChoices(select, menu, allowOther) {
  const value = select.value;
  select.replaceChildren(new Option("Choose an item", ""));
  const community = document.createElement("optgroup");
  community.label = "Community";
  for (const item of menu) {
    const label = Number.isFinite(item.price) ? `${item.name} · ${money.format(item.price)}` : item.name;
    if (isOfficialDish(item)) {
      select.add(new Option(label, item.item_id));
    } else if (allowOther && item.community === true && (item.item_id === null || item.item_id < 0) && Number.isFinite(item.price)) {
      const option = new Option(`${label} (community)${Number.isInteger(item.votes) ? ` · ${item.votes} reports` : ""}`, menuValue(item));
      option.dataset.foodName = item.name;
      option.dataset.priceCents = Math.round(item.price * 100);
      community.append(option);
    }
  }
  if (community.childElementCount) select.append(community);
  if (allowOther) select.add(new Option("Other", "other"));
  if ([...select.options].some((option) => option.value === value)) select.value = value;
  const note = select.nextElementSibling;
  select.onchange = () => {
    note.textContent = communityPriceNote(menu.find(({ item_id }) => String(item_id) === select.value));
  };
  select.onchange();
}

function updateFoodButtons() {
  byId("addFoodItem").disabled = foodItems.childElementCount >= 20;
  byId("removeFoodItem").hidden = foodItems.childElementCount < 2;
}

function addFoodItem(focus = false) {
  const row = element("div", "", foodItems);
  const label = element("label", `Item ${foodItems.childElementCount}`, row);
  const select = element("select", "", label);
  select.required = true;
  const note = element("small", "", label);
  note.className = "hint menu-price-note";
  note.id = `foodItemPrice${foodItems.childElementCount}`;
  note.setAttribute("role", "status");
  select.setAttribute("aria-describedby", note.id);
  menuChoices(select, feedback.menu, true);
  const custom = element("div", "", row);
  custom.hidden = true;
  const input = element("input", "", element("label", "Other food name", custom));
  input.maxLength = 80;
  const suggestions = element("div", "", custom);
  suggestions.className = "name-suggestions";
  const correction = element("div", "", custom);
  correction.className = "official-suggestion";
  const updateSuggestions = () => {
    suggestions.replaceChildren();
    const query = nameTokens(input.value);
    if (select.value !== "other" || query.join("").length < 2) return;
    const matches = feedback.menu.filter((item) => [item.name, ...(item.aliases ?? [])]
      .some((name) => matchesMenuName(query, name)));
    for (const item of matches.slice(0, 5)) {
      const suffix = item.community && Number.isInteger(item.votes) ? ` · ${item.votes} reports` : "";
      const button = element("button", item.name + suffix, suggestions);
      button.type = "button";
      button.addEventListener("click", () => chooseMenuItem(select, item));
    }
  };
  refreshSuggestions.set(select, updateSuggestions);
  input.addEventListener("input", () => {
    correction.replaceChildren();
    updateSuggestions();
  });
  const price = element("input", "", element("label", "Price ($)", custom));
  price.type = "number";
  price.min = "0.01";
  price.max = "1000";
  price.step = "0.01";
  const hint = element("small", "Menu price before tax and tip.", custom);
  hint.className = "hint";
  hint.id = `otherPriceHint${foodItems.childElementCount}`;
  const error = element("p", "", custom);
  error.className = "field-error";
  error.id = `otherPriceError${foodItems.childElementCount}`;
  error.setAttribute("role", "alert");
  price.setAttribute("aria-describedby", `${hint.id} ${error.id}`);

  const showPriceError = () => {
    const invalid = !price.validity.valid;
    error.textContent = invalid ? "Enter a price from $0.01 to $1,000 with up to two decimal places." : "";
    if (invalid) price.setAttribute("aria-invalid", "true");
    else price.removeAttribute("aria-invalid");
  };
  price.addEventListener("invalid", showPriceError);
  price.addEventListener("input", () => {
    if (price.hasAttribute("aria-invalid")) showPriceError();
  });
  select.addEventListener("change", () => {
    const other = select.value === "other";
    custom.hidden = !other;
    input.required = price.required = other;
    input.disabled = price.disabled = !other;
    correction.replaceChildren();
    updateSuggestions();
    if (!other) showPriceError();
  });
  input.disabled = price.disabled = true;
  updateFoodButtons();
  if (focus) select.focus();
}

byId("addFoodItem").addEventListener("click", () => addFoodItem(true));
byId("removeFoodItem").addEventListener("click", () => {
  foodItems.lastElementChild.remove();
  updateFoodButtons();
  foodItems.lastElementChild.querySelector("select").focus();
});

function resetPreview() {
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  photoUrl = null;
  byId("foodPreview").removeAttribute("src");
  byId("foodPreview").hidden = true;
}
photoInput.addEventListener("change", () => {
  resetPreview();
  const file = photoInput.files[0];
  const error = file?.size > 5000000 ? "Choose a photo under 5 MB." :
    file?.type && !["image/jpeg", "image/png", "image/webp"].includes(file.type)
      ? "Choose a JPEG, PNG, or WebP image." : "";
  photoInput.setCustomValidity(error);
  setText("foodStatus", error);
  if (file && !error) {
    photoUrl = URL.createObjectURL(file);
    byId("foodPreview").src = photoUrl;
    byId("foodPreview").hidden = false;
  }
});
foodDialog.addEventListener("close", () => {
  if (!foodDialog.open) resetPreview();
});

byId("logFood").addEventListener("click", async (event) => {
  const place = pickedPlace;
  if (!place || foodPending || pricePending) return;
  const context = { place, menu: [...(knownMenus.get(place.id)?.values() ?? [])] };
  feedback = context;
  foodForm.reset();
  photoInput.setCustomValidity("");
  resetPreview();
  foodItems.replaceChildren();
  addFoodItem();
  setText("foodRestaurant", place.name);
  setText("foodStatus", visited(place) ? "" : "Log your visit first to save food. You can still report a problem.");
  setText("saveFood", stamped(place) ? "Save food" : "Save food and earn stamp");
  byId("saveFood").disabled = !visited(place);
  byId("openPriceReport").disabled = false;
  setText("menuStatus", "Loading menu…");
  openDialog(foodDialog, event.currentTarget);
  try {
    const { items } = await get("/api/menu", { place_id: place.id });
    if (feedback !== context || !foodDialog.open) return;
    context.menu = items;
    for (const select of foodItems.querySelectorAll("select")) {
      menuChoices(select, items, true);
      refreshSuggestions.get(select)?.();
    }
    byId("openPriceReport").disabled = foodPending;
    setText("menuStatus", items.length ? "" : "No menu items are available. Choose Other to log food.");
  } catch (error) {
    if (feedback === context) setText("menuStatus", error.message);
  }
});

foodForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (foodPending) return;
  const { place } = feedback;
  const submittedRows = [...foodItems.children];
  for (const row of submittedRows) row.querySelector(".official-suggestion").replaceChildren();
  const items = submittedRows.map((row) => {
    const select = row.querySelector("select");
    if (select.value === "other") {
      const [name, price] = row.querySelectorAll("input");
      return { other: name.value.trim(), price_cents: Math.round(price.valueAsNumber * 100) };
    }
    const option = select.selectedOptions[0];
    if (option.dataset.foodName !== undefined) {
      return { other: option.dataset.foodName, price_cents: Number(option.dataset.priceCents) };
    }
    return { item_id: Number(select.value) };
  });
  if (items.some((item) => item.other === "")) {
    setText("foodStatus", "Enter a name for each Other food item.");
    return;
  }
  const body = new FormData(foodForm);
  if (!photoInput.files.length) body.delete("photo");
  body.set("place_id", place.id);
  body.set("items", JSON.stringify(items));
  foodPending = byId("saveFood").disabled = true;
  byId("openPriceReport").disabled = true;
  setText("foodStatus", "Saving your food…");
  try {
    const data = await postCommunity("/api/food-log", body);
    accountVersion++;
    account.points = data.points;
    if (data.stamp_earned) {
      account.stamps.push({ place_id: place.id, name: place.name, earned_at: new Date().toISOString() });
      account.stamp_count++;
      document.dispatchEvent(new Event("community-checkin"));
    }
    renderProfile();
    renderSelectedDetails();
    refreshAccount();
    communityMenuRequests.delete(place.id);
    schedulePlaces();
    scheduleLateStrip();
    foodForm.reset();
    resetPreview();
    foodItems.replaceChildren();
    addFoodItem();
    setText("saveFood", "Save food");
    setText("foodStatus", data.stamp_earned ? "Food saved. You earned a stamp!" : "Food saved.");
  } catch (error) {
    setText("foodStatus", error.message);
    if (foodDialog.open) showOfficialSuggestion(submittedRows, items, error.suggest);
  } finally {
    foodPending = byId("saveFood").disabled = false;
    byId("openPriceReport").disabled = false;
    if (foodDialog.open && !priceDialog.open && document.activeElement === document.body) byId("saveFood").focus();
  }
});

const reportSelect = byId("reportedDish");
const removeItemForm = byId("removeItemForm");
const reportMode = () => priceDialog.querySelector('[name="reportAction"]:checked');
let reporting;

function canReportItem(item) {
  return Number.isInteger(item?.item_id) && item.item_id !== 0;
}

function selectedReportItem() {
  return reporting?.items.find((item) => String(item.item_id) === reportSelect.value && !item.removed);
}

function renderReportFields() {
  const item = selectedReportItem();
  const details = byId("reportItemDetails");
  details.replaceChildren();
  if (item) {
    if (Number.isFinite(item.price)) {
      if (item.community) element("p", `Diner-reported price: ${money.format(item.price)}`, details);
      else {
        element("p", `Menu price: ${money.format(item.price_original ?? item.price)}`, details);
        if (Number.isFinite(item.price_original)) element("p", `Community price: ${money.format(item.price)}`, details);
      }
    }
    if (item.remove_votes > 0) {
      element("p", `${item.remove_votes} ${item.remove_votes === 1 ? "report" : "reports"} that it isn’t on the menu`, details);
    }
    if (Number.isInteger(item.price_reports) && Number.isInteger(item.price_reports_needed)) {
      element("p", `${item.price_reports} of ${item.price_reports_needed} matching price reports`, details);
    }
    if (item.my_report) element("p", item.my_report === "remove" ? "You reported this item." : "You marked this item as back.", details);
    if (!isOfficialDish(item)) element("p", "Wrong-price reports are available for official menu items.", details);
  }
  const priceOption = priceDialog.querySelector('[name="reportAction"][value="price"]');
  const removeOption = priceDialog.querySelector('[name="reportAction"][value="remove"]');
  priceOption.disabled = !isOfficialDish(item) || pricePending;
  removeOption.disabled = !item || pricePending;
  for (const option of priceDialog.querySelectorAll('[name="reportAction"][value="closed"], [name="reportAction"][value="timing"]')) {
    option.disabled = pricePending;
  }
  const mode = reportMode().value;
  const placeMode = mode === "closed" || mode === "timing";
  if (item && !isOfficialDish(item) && mode === "price") removeOption.checked = true;
  const remove = reportMode().value === "remove";
  // Restaurant reports are about the place, so the menu item does not apply to them.
  byId("reportItemField").hidden = placeMode;
  byId("reportItemDetails").hidden = placeMode;
  renderPlaceReportDetails(placeMode);
  priceForm.hidden = !item || remove || placeMode;
  removeItemForm.hidden = !item || !remove;
  byId("closedPlaceForm").hidden = mode !== "closed";
  byId("timingPlaceForm").hidden = mode !== "timing";
  byId("submitPrice").disabled = pricePending || !isOfficialDish(item);
  byId("submitRemoval").disabled = pricePending || !item;
  byId("submitClosedPlace").disabled = pricePending || !reporting;
  byId("submitTimingPlace").disabled = pricePending || !reporting;
  reportSelect.disabled = pricePending || !reporting?.items.some((entry) => !entry.removed && canReportItem(entry));
  for (const button of byId("hiddenItems").querySelectorAll("button")) button.disabled = pricePending;
}

function renderPlaceReportDetails(visible) {
  const details = byId("reportPlaceDetails");
  details.replaceChildren();
  const report = reporting?.placeReport;
  details.hidden = !visible || !report;
  if (!visible || !report) return;
  const needed = report.community_report_needed ?? 2;
  if (report.community_closed) element("p", "Diners reported this restaurant closed.", details);
  else if (report.community_closed_votes > 0) {
    element("p", `${report.community_closed_votes} of ${needed} reports that it’s closed`, details);
  }
  if (report.community_timing) element("p", `Hours from diners: ${report.community_timing}`, details);
  else if (report.community_timing_votes > 0) {
    element("p", `${report.community_timing_votes} of ${needed} matching reports of new hours`, details);
  }
}

function renderReportMenu(preferred) {
  const items = reporting.items.filter(canReportItem);
  reportSelect.replaceChildren(new Option("Choose an item", ""));
  for (const item of items.filter((entry) => !entry.removed)) {
    reportSelect.add(new Option(item.name, item.item_id));
  }
  const wanted = preferred ?? (reportSelect.options.length === 2 ? reportSelect.options[1].value : "");
  if ([...reportSelect.options].some((option) => option.value === String(wanted))) reportSelect.value = wanted;
  const hiddenItems = byId("hiddenItems");
  hiddenItems.replaceChildren();
  for (const item of items.filter((entry) => entry.removed)) {
    const row = element("li", "", hiddenItems);
    element("span", item.name, row);
    const button = element("button", "It's back", row);
    button.type = "button";
    button.setAttribute("aria-label", `It's back: ${item.name}`);
    button.addEventListener("click", () => sendItemReport(item, "restore", undefined, button));
  }
  byId("hiddenByReports").hidden = !hiddenItems.childElementCount;
  renderReportFields();
}

function updateFoodReportMenu(placeId, items) {
  if (feedback?.place.id !== placeId) return;
  feedback.menu = items.filter((item) => !item.removed);
  for (const select of foodItems.querySelectorAll("select")) {
    menuChoices(select, feedback.menu, true);
    refreshSuggestions.get(select)?.();
  }
}

async function refreshReportMenu(context, preferred) {
  const menu = await get("/api/menu", { place_id: context.place.id, include_removed: 1 });
  const { items } = menu;
  if (reporting !== context) return;
  context.items = items;
  context.placeReport = "community_closed_votes" in menu ? menu : null;
  updateFoodReportMenu(context.place.id, items);
  setText("reportMenuStatus", "");
  renderReportMenu(preferred);
}

function openReport(place, trigger, itemId, fallback) {
  if (!place || pricePending) return;
  const context = { place, items: fallback };
  reporting = context;
  priceForm.reset();
  removeItemForm.reset();
  byId("closedPlaceForm").reset();
  byId("timingPlaceForm").reset();
  priceDialog.querySelector('[name="reportAction"][value="price"]').checked = true;
  byId("hiddenByReports").open = false;
  setText("priceStatus", "");
  setText("reportMenuStatus", "Loading menu…");
  renderReportMenu(itemId);
  openDialog(priceDialog, trigger);
  (reportSelect.disabled ? priceDialog.querySelector("[data-close]") : reportSelect).focus();
  refreshReportMenu(context, itemId).catch((error) => {
    if (reporting === context && priceDialog.open) setText("reportMenuStatus", error.message);
  });
}

reportSelect.addEventListener("change", () => {
  setText("priceStatus", "");
  priceForm.reset();
  removeItemForm.reset();
  renderReportFields();
});
byId("reportChoices").addEventListener("change", renderReportFields);
byId("openPriceReport").addEventListener("click", (event) => {
  openReport(feedback.place, event.currentTarget, undefined, feedback.menu);
});
byId("heroReport").addEventListener("click", (event) => {
  if (!currentDish) return;
  openReport(currentDish.place, event.currentTarget, currentDish.item_id,
    [...(knownMenus.get(currentDish.place.id)?.values() ?? [])]);
});

async function sendItemReport(item, action, reason, trigger) {
  if (pricePending) return;
  const context = reporting;
  pricePending = true;
  renderReportFields();
  setText("priceStatus", "Submitting your report…");
  try {
    const data = await postCommunity("/api/item-report", { place_id: context.place.id, item_id: item.item_id, action, reason });
    Object.assign(item, { removed: data.removed, remove_votes: data.remove_votes, my_report: action });
    const message = action === "restore" ? (data.removed ? "Thanks. Your restore report was saved." : "Thanks. It’s back on the menu.") :
      data.removed ? `Hidden after ${data.remove_votes} reports.` :
      data.remove_votes === data.needed - 1 ? "Thanks. One more report hides it." : "Thanks. Your report was saved.";
    setText("priceStatus", message);
    if (data.removed) knownMenus.get(context.place.id)?.delete(item.item_id);
    updateFoodReportMenu(context.place.id, context.items);
    communityMenuRequests.delete(context.place.id);
    schedulePlaces();
    scheduleLateStrip();
    renderReportMenu(item.item_id);
    if (data.removed) replaceRemovedDish(context.place.id, item.item_id);
    await refreshReportMenu(context, item.item_id).catch((error) => setText("reportMenuStatus", error.message));
  } catch (error) {
    setText("priceStatus", error.message);
  } finally {
    pricePending = false;
    renderReportFields();
    if (priceDialog.open && (document.activeElement === document.body || !document.activeElement.isConnected)) {
      if (trigger.isConnected && !trigger.disabled && !trigger.closest("[hidden]")) trigger.focus();
      else if (!reportSelect.disabled) reportSelect.focus();
      else priceDialog.querySelector("[data-close]").focus();
    }
  }
}
removeItemForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const item = selectedReportItem();
  if (item) sendItemReport(item, "remove", byId("removeReason").value, byId("submitRemoval"));
});

function placeReportThanks(action, data) {
  if (action === "closed") {
    if (data.closed) return `Hidden after ${data.closed_votes} reports that it’s closed.`;
    return data.closed_votes === data.needed - 1 ? "Thanks. One more report hides this restaurant."
      : "Thanks. Your report was saved.";
  }
  if (data.timing) return `Hours updated after ${data.timing_votes} matching reports.`;
  return data.timing_votes === data.needed - 1 ? "Thanks. One more matching report updates the hours."
    : "Thanks. Your hours were saved.";
}

async function sendPlaceReport(action, value, trigger) {
  if (pricePending || !reporting) return;
  const context = reporting;
  const placeId = context.place.id;
  pricePending = true;
  renderReportFields();
  setText("priceStatus", "Submitting your report…");
  try {
    const data = await postCommunity("/api/place-report", { place_id: placeId, action, value });
    context.placeReport = {
      community_closed: data.closed, community_closed_votes: data.closed_votes, community_open_votes: data.open_votes,
      community_timing: data.timing, community_timing_votes: data.timing_votes, community_report_needed: data.needed
    };
    setText("priceStatus", placeReportThanks(action, data));
    if (action === "timing") byId("timingPlaceForm").reset();
    if (data.timing && pickedPlace?.id === placeId) {
      pickedPlace.hours_text = data.timing;
      renderSelectedDetails();
    }
    schedulePlaces();
    scheduleLateStrip();
    if (data.closed) replaceClosedPlace(placeId);
  } catch (error) {
    setText("priceStatus", error.message);
  } finally {
    pricePending = false;
    renderReportFields();
    if (priceDialog.open && (document.activeElement === document.body || !document.activeElement?.isConnected
        || document.activeElement.disabled)) {
      (trigger.isConnected && !trigger.disabled ? trigger : priceDialog.querySelector("[data-close]")).focus();
    }
  }
}

byId("closedPlaceForm").addEventListener("submit", (event) => {
  event.preventDefault();
  sendPlaceReport("closed", undefined, byId("submitClosedPlace"));
});
byId("timingPlaceForm").addEventListener("submit", (event) => {
  event.preventDefault();
  sendPlaceReport("timing", byId("observedTiming").value, byId("submitTimingPlace"));
});

function priceReportThanks(item, cents, original, previous, effective) {
  const largeChange = Math.abs((effective ?? cents) - original) > Math.max(200, .5 * original);
  const needed = item?.price_reports_needed ?? (largeChange ? 3 : 2);
  const count = item?.price_reports ?? 0;
  if (Number.isInteger(effective) && previous !== effective && Math.abs(cents - effective) <= 40) {
    return `Price updated after ${needed} reports.`;
  }
  if (count < needed) {
    if (needed === 3) return `Thanks. This needs 3 reports because it is a big change (${count} of 3 matching).`;
    if (needed - count === 1) return "Thanks. One more matching report updates the price.";
  }
  return "Thanks. Your price report was saved.";
}

priceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const item = selectedReportItem();
  if (pricePending || !isOfficialDish(item)) return;
  const context = reporting;
  const cents = Math.round(Number(byId("reportedPrice").value) * 100);
  const original = Math.round((item.price_original ?? item.price) * 100);
  const previous = Number.isFinite(item.price_original) ? Math.round(item.price * 100) : undefined;
  pricePending = true;
  renderReportFields();
  setText("priceStatus", "Submitting your report…");
  try {
    const data = await postCommunity("/api/price-report", {
      place_id: context.place.id, item_id: item.item_id, cents
    });
    accountVersion++;
    account.points = data.points;
    renderProfile();
    refreshAccount();
    const scoped = data.prices_by_place?.[context.place.id] ?? data.prices ?? {};
    const effective = scoped[item.item_id];
    if (currentDish?.place.id === context.place.id && currentDish.item_id === item.item_id) {
      const original = currentDish.price_original ?? currentDish.price;
      currentDish.price = Number.isInteger(effective) ? effective / 100 : original;
      currentDish.price_original = Number.isInteger(effective) ? original : undefined;
      currentDish.price_status = Number.isInteger(effective) ? "reported" : item.price_status === "reported" ? "ok" : item.price_status;
      currentDish.total_cost = Math.round((currentDish.price + (currentDish.fare ?? 0)) * 100) / 100;
    }
    applyCommunityPrices(data, context.place.id);
    setText("priceStatus", "Thanks. Your price report was saved.");
    await refreshReportMenu(context, item.item_id);
    setText("priceStatus", priceReportThanks(selectedReportItem(), cents, original, previous, effective));
  } catch (error) {
    setText("priceStatus", error.message);
  } finally {
    pricePending = false;
    renderReportFields();
    if (priceDialog.open && document.activeElement === document.body && !byId("submitPrice").disabled) byId("submitPrice").focus();
  }
});

renderProfile();
refreshAccount();
get("/api/menu-prices").then((response) => {
  if (priceVersion === 0) applyCommunityPrices(response);
}).catch(() => {
  // Spin results also carry the effective price when a consensus applies.
});
