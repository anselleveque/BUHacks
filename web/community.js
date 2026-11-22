"use strict";

const profileKey = "midnight-munch-profile";
const avatarColors = {
  red: "#e9918a", yellow: "#f7db7e", green: "#acd3a0", blue: "#a4c9ec",
  purple: "#cab5e8", pink: "#f3c0d3", white: "#fff"
};
const avatarChoices = [...byId("avatar").options].map((option) => option.value);
let profile = { name: "", avatar: "🐶", color: "blue", bio: "" };
try {
  const current = localStorage.getItem(profileKey);
  const legacy = current === null ? localStorage.getItem("bite-boston-profile") : null;
  const saved = JSON.parse(current ?? legacy);
  if (saved) profile = {
    name: typeof saved.name === "string" ? saved.name.slice(0, 28) : "",
    bio: typeof saved.bio === "string" ? saved.bio.slice(0, 140) : "",
    avatar: avatarChoices.includes(saved.avatar) ? saved.avatar : profile.avatar,
    color: Object.hasOwn(avatarColors, saved.color) ? saved.color : profile.color
  };
  if (legacy !== null) localStorage.setItem(profileKey, JSON.stringify(profile));
} catch {
  // The profile starts with defaults if storage is unavailable.
}

const pointLabels = { total: "Total", visits: "Visits", stamps: "Stamps", food_logs: "Food logs", price_reports: "Price reports" };
let account = { visits: [], stamps: [], stamp_count: 0, points: {}, food_logs: [] };
let accountVersion = 0;
let communityPrices = {};
let priceVersion = 0;
const knownMenus = new Map();
const visitPending = new Set();
const visitMessages = new Map();
const demoCheckIn = new URLSearchParams(location.search).get("demo") === "1";

const visited = (place) => account.visits.some(({ place_id }) => place_id === place.id) || place.visited === true;
const stamped = (place) => account.stamps.some(({ place_id }) => place_id === place.id) || place.stamped === true;

function stampIcon(place) {
  const earned = stamped(place);
  const node = iconText(earned ? "🍓" : "🌸");
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
  setText("profileScore", `🍓 ${account.stamp_count} · ${account.points.total ?? 0} pts`);
  byId("profileScore").setAttribute("aria-label", `${account.stamp_count} stamps, ${account.points.total ?? 0} points`);
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
  const points = byId("pointBreakdown");
  points.replaceChildren();
  for (const [key, label] of Object.entries(pointLabels)) {
    element("dt", label, points);
    element("dd", account.points[key] ?? 0, points);
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
}

async function refreshAccount() {
  const version = ++accountVersion;
  try {
    const data = await get("/api/me");
    if (version !== accountVersion) return;
    account = data;
    setText("accountStatus", "");
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
for (const dialog of document.querySelectorAll("dialog")) {
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
function previewAvatar() {
  paintAvatar(byId("avatarPreview"), byId("avatar").value, byId("profileColor").value);
}
byId("profileOpen").addEventListener("click", (event) => {
  byId("displayName").value = profile.name;
  byId("avatar").value = profile.avatar;
  byId("profileColor").value = profile.color;
  byId("bio").value = profile.bio;
  setText("profileStatus", "");
  previewAvatar();
  openDialog(profileDialog, event.currentTarget);
  refreshAccount();
});
byId("avatar").addEventListener("change", previewAvatar);
byId("profileColor").addEventListener("change", previewAvatar);
byId("profileForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const next = {
    name: byId("displayName").value.trim(), avatar: byId("avatar").value,
    color: byId("profileColor").value, bio: byId("bio").value.trim()
  };
  try {
    localStorage.setItem(profileKey, JSON.stringify(next));
  } catch {
    setText("profileStatus", "This browser could not save your profile.");
    return;
  }
  profile = next;
  renderProfile();
  renderOrigin();
  profileDialog.close();
});

function renderSelectedDetails() {
  const place = pickedPlace;
  if (!place) return;
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
      if (!navigator.geolocation || !window.isSecureContext) {
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

function renderDishPrice(dish, startingPrice = /^sizes:/i.test(dish.price_note?.trim() ?? "") ||
    dish.notes?.trim().toLowerCase() === "from") {
  const cents = communityPrices[dish.item_id];
  const original = dish.price_original ?? dish.price;
  const hasConsensus = Number.isInteger(cents);
  const price = hasConsensus ? cents / 100 : dish.price;
  setText("price", `${startingPrice ? "from " : ""}${money.format(price)}`);
  const showFare = subwayFare !== undefined && locationPoint && locationPoint === dishFareStart &&
    dish.fare_included === true && Number.isFinite(dish.fare) && Number.isFinite(dish.total_cost);
  const breakdown = byId("fareBreakdown");
  breakdown.hidden = !showFare;
  if (showFare) {
    const total = price === dish.price ? dish.total_cost : price + dish.fare;
    const saved = activeWalk ? (dish.walk_saves || dish.fare || 0) : 0;
    breakdown.textContent = activeWalk || dish.mode === "walk" || !dish.fare
      ? `Walkable, no fare${saved ? ` · saves ${money.format(saved)} · ${money.format(price)} total` : ""}`
      : `+ ${money.format(dish.fare)} ${fareKind(dish.fare_trips)} T fare${dish.fare_trips === 2 && Number.isFinite(dish.fare_each)
        ? ` (${money.format(dish.fare_each)} each way)` : ""} = ${money.format(total)} total · ${transferNote()}`;
  }
  setText("resultBudget", locationPoint && activeWalk ? `Budget $${budget.value} · dish only on foot` : "");
  setText("communityPrice", dish.community === true ? "price reported by diners" : communityPriceNote(hasConsensus
    ? { price_status: "reported", price_original: original } : dish));
}

function applyCommunityPrices(prices) {
  priceVersion++;
  communityPrices = prices;
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
  if (item && !isOfficialDish(item)) removeOption.checked = true;
  const remove = reportMode().value === "remove";
  priceForm.hidden = !item || remove;
  removeItemForm.hidden = !item || !remove;
  byId("submitPrice").disabled = pricePending || !isOfficialDish(item);
  byId("submitRemoval").disabled = pricePending || !item;
  reportSelect.disabled = pricePending || !reporting?.items.some((entry) => !entry.removed && canReportItem(entry));
  for (const button of byId("hiddenItems").querySelectorAll("button")) button.disabled = pricePending;
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
  const { items } = await get("/api/menu", { place_id: context.place.id, include_removed: 1 });
  if (reporting !== context) return;
  context.items = items;
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
    const effective = data.prices[item.item_id];
    const prices = { ...communityPrices };
    if (Number.isInteger(effective)) prices[item.item_id] = effective;
    else delete prices[item.item_id];
    applyCommunityPrices(prices);
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
get("/api/menu-prices").then(({ prices }) => {
  if (priceVersion === 0) applyCommunityPrices(prices);
}).catch(() => {
  // Spin results also carry the effective price when a consensus applies.
});
