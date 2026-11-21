"use strict";

const profileKey = "bite-boston-profile";
const avatarColors = {
  red: "#e9918a", yellow: "#f7db7e", green: "#acd3a0", blue: "#a4c9ec",
  purple: "#cab5e8", pink: "#f3c0d3", white: "#fff"
};
const avatarChoices = [...byId("avatar").options].map((option) => option.value);
let profile = { name: "", avatar: "🐶", color: "blue", bio: "" };
try {
  const saved = JSON.parse(localStorage.getItem(profileKey));
  if (saved) profile = {
    name: typeof saved.name === "string" ? saved.name.slice(0, 28) : "",
    bio: typeof saved.bio === "string" ? saved.bio.slice(0, 140) : "",
    avatar: avatarChoices.includes(saved.avatar) ? saved.avatar : profile.avatar,
    color: Object.hasOwn(avatarColors, saved.color) ? saved.color : profile.color
  };
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
  } else if (Number.isFinite(fromPrice)) element("p", `From ${money.format(fromPrice)}`, details);
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
    : visitMessages.get(place.id) || (hasCoordinates(place)
      ? "Tap Log restaurant to check your GPS location." : "Location coordinates are unavailable for this restaurant."));
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
  if (!response.ok || result.error) throw new Error(result.error || "The request couldn’t be saved. Try again.");
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
    price_original: dish.price_original, price_status: dish.price_status
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
    breakdown.textContent = dish.fare > 0
      ? `${money.format(price)} dish + ${money.format(dish.fare)} T fare = ${money.format(total)}`
      : `${money.format(price)} dish · walkable, no fare`;
  }
  setText("communityPrice", communityPriceNote(hasConsensus
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

const isOfficialDish = (item) => Number.isInteger(item.item_id) && !item.community;

function menuChoices(select, menu, allowOther) {
  const value = select.value;
  select.replaceChildren(new Option("Choose an item", ""));
  const community = document.createElement("optgroup");
  community.label = "Community";
  for (const item of menu) {
    const label = Number.isFinite(item.price) ? `${item.name} · ${money.format(item.price)}` : item.name;
    if (isOfficialDish(item)) {
      select.add(new Option(label, item.item_id));
    } else if (allowOther && item.community === true && item.item_id === null && Number.isFinite(item.price)) {
      const option = new Option(`${label} (community)`, `community:${item.name}`);
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
  setText("foodStatus", visited(place) ? "" : "Log your visit first to save food. You can still report a price.");
  setText("saveFood", stamped(place) ? "Save food" : "Save food and earn stamp");
  byId("saveFood").disabled = !visited(place);
  byId("openPriceReport").disabled = !context.menu.some(isOfficialDish);
  setText("menuStatus", "Loading menu…");
  openDialog(foodDialog, event.currentTarget);
  try {
    const { items } = await get("/api/menu", { place_id: place.id });
    if (feedback !== context || !foodDialog.open) return;
    context.menu = items;
    for (const select of foodItems.querySelectorAll("select")) menuChoices(select, items, true);
    if (priceDialog.open) menuChoices(byId("reportedDish"), items, false);
    byId("openPriceReport").disabled = foodPending || !items.some(isOfficialDish);
    setText("menuStatus", items.length ? "" : "No menu items are available. Choose Other to log food.");
  } catch {
    if (feedback === context) setText("menuStatus", context.menu.length
      ? "Full menu unavailable. You can use dishes already shown or choose Other."
      : "Menu unavailable. Choose Other to log food.");
  }
});

foodForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (foodPending) return;
  const { place } = feedback;
  const items = [...foodItems.children].map((row) => {
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
    foodForm.reset();
    resetPreview();
    foodItems.replaceChildren();
    addFoodItem();
    setText("saveFood", "Save food");
    setText("foodStatus", data.stamp_earned ? "Food saved. You earned a stamp!" : "Food saved.");
  } catch (error) {
    setText("foodStatus", error.message);
  } finally {
    foodPending = byId("saveFood").disabled = false;
    byId("openPriceReport").disabled = !feedback.menu.some(isOfficialDish);
    if (foodDialog.open && !priceDialog.open && document.activeElement === document.body) byId("saveFood").focus();
  }
});

byId("openPriceReport").addEventListener("click", (event) => {
  priceForm.reset();
  menuChoices(byId("reportedDish"), feedback.menu, false);
  setText("priceStatus", "");
  openDialog(priceDialog, event.currentTarget);
  byId("reportedDish").focus();
});

priceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pricePending) return;
  pricePending = byId("submitPrice").disabled = true;
  setText("priceStatus", "Submitting your report…");
  try {
    const data = await postCommunity("/api/price-report", {
      place_id: feedback.place.id,
      item_id: Number(byId("reportedDish").value),
      cents: Math.round(Number(byId("reportedPrice").value) * 100)
    });
    setText("priceStatus", "Price report saved.");
    accountVersion++;
    account.points = data.points;
    renderProfile();
    refreshAccount();
    applyCommunityPrices(data.prices);
  } catch (error) {
    setText("priceStatus", error.message);
  } finally {
    pricePending = byId("submitPrice").disabled = false;
    if (priceDialog.open && document.activeElement === document.body) byId("submitPrice").focus();
  }
});

renderProfile();
refreshAccount();
get("/api/menu-prices").then(({ prices }) => {
  if (priceVersion === 0) applyCommunityPrices(prices);
}).catch(() => {
  // Spin results also carry the effective price when a consensus applies.
});
