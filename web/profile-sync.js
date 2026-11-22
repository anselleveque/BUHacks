"use strict";

// Round 30: save the profile on the server, recovery codes, restore, nudges and the completeness meter.
// localStorage keeps the offline copy of the profile (community.js); this file syncs it with
// GET/PUT /api/profile. A recovery code lives only in memory while it is shown, never in storage or logs.

const syncKey = "midnight-munch-profile-sync";
const nudgeKey = "midnight-munch-nudges";
const nudgeSessionKey = "midnight-munch-nudge-session";
const nudgeCooldown = 3 * 24 * 60 * 60 * 1000;
const nudgeSnooze = 7 * 24 * 60 * 60 * 1000;
const memoryStore = new Map();

function storageWorks(area) {
  try {
    const storage = area();
    const probe = "midnight-munch-probe";
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return true;
  } catch {
    return false;
  }
}
const localOk = storageWorks(() => window.localStorage);
// Read before anything else writes: a missing copy means this browser's profile was cleared.
const hadLocalProfile = (() => {
  try { return localStorage.getItem(profileKey) !== null || localStorage.getItem("bite-boston-profile") !== null; }
  catch { return false; }
})();

function readStored(key, session = false) {
  try {
    const value = JSON.parse((session ? sessionStorage : localStorage).getItem(key));
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    // Fall back to this page's memory below.
  }
  return memoryStore.get(key) ?? {};
}
function writeStored(key, value, session = false) {
  memoryStore.set(key, value);
  try { (session ? sessionStorage : localStorage).setItem(key, JSON.stringify(value)); } catch { /* Memory only. */ }
}

// sync: { base: server updated_at this copy matches, dirty: local edits not yet on the server,
//         consent: true | false | undefined, code_saved_at, avatar_chosen }
let sync = readStored(syncKey);
const setSync = (changes) => { sync = { ...sync, ...changes }; writeStored(syncKey, sync); };
let serverState = { known: false, exists: false, has_recovery_code: false, updated_at: null };
let foundProfile = null;
let syncTimer;
let syncChain = Promise.resolve();

const setDefault = (target, key, value) => { if (value !== undefined && value !== null && value !== "") target[key] = value; };
const wholeNumber = (value) => {
  const number = Number(value);
  return value !== "" && value !== null && Number.isInteger(number) ? number : undefined;
};
const choiceList = (value) => Array.isArray(value) && value.length ? [...new Set(value.filter((item) => typeof item === "string"))] : undefined;

// The browser stores select values; the server stores typed settings with its own key names.
function profileForServer(local = profile) {
  const saved = local.defaults ?? {};
  const defaults = {};
  setDefault(defaults, "home_station_id", typeof saved.station === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(saved.station) ? saved.station : undefined);
  setDefault(defaults, "budget", wholeNumber(saved.budget));
  setDefault(defaults, "stay_min", wholeNumber(saved.stay));
  setDefault(defaults, "walk_ok_min", wholeNumber(saved.walk));
  if (saved.travel === "any") defaults.max_travel_min = null;
  else setDefault(defaults, "max_travel_min", wholeNumber(saved.travel));
  setDefault(defaults, "areas", choiceList(saved.areas)?.slice(0, 12));
  setDefault(defaults, "late_mode", ["auto", "on", "off"].includes(saved.late) ? saved.late : undefined);
  if (saved.rideHome === "yes" || saved.rideHome === "no") defaults.count_ride_home = saved.rideHome === "yes";
  setDefault(defaults, "diets", choiceList(saved.diet));
  setDefault(defaults, "cravings", choiceList(saved.craving));
  try {
    const theme = localStorage.getItem("midnight-munch-theme");
    if (theme === "light" || theme === "dark") defaults.theme = theme;
  } catch { /* The theme stays local. */ }
  return { display_name: local.name ?? "", avatar: local.avatar, color: local.color, bio: local.bio ?? "", defaults };
}

function profileFromServer(saved) {
  const fresh = freshProfile();
  if (!saved) return fresh;
  const server = saved.defaults ?? {};
  const defaults = {};
  setDefault(defaults, "station", server.home_station_id);
  for (const [local, key] of [["budget", "budget"], ["stay", "stay_min"], ["walk", "walk_ok_min"]]) {
    if (Number.isInteger(server[key])) defaults[local] = String(server[key]);
  }
  if ("max_travel_min" in server) defaults.travel = server.max_travel_min === null ? "any" : String(server.max_travel_min);
  for (const [local, key] of [["areas", "areas"], ["diet", "diets"], ["craving", "cravings"]]) {
    if (Array.isArray(server[key])) defaults[local] = server[key].filter((item) => typeof item === "string");
  }
  setDefault(defaults, "late", server.late_mode);
  if (typeof server.count_ride_home === "boolean") defaults.rideHome = server.count_ride_home ? "yes" : "no";
  if (server.theme === "light" || server.theme === "dark") defaults.theme = server.theme;
  return {
    name: typeof saved.display_name === "string" ? saved.display_name.slice(0, 28) : "",
    bio: typeof saved.bio === "string" ? saved.bio.slice(0, 140) : "",
    avatar: avatarChoices.includes(saved.avatar) ? saved.avatar : fresh.avatar,
    color: Object.hasOwn(avatarColors, saved.color) ? saved.color : fresh.color,
    defaults
  };
}

const sameProfile = (left, right) => JSON.stringify(profileForServer(left)) === JSON.stringify(profileForServer(right));

function showToast(message) {
  const toast = byId("profileToast");
  toast.hidden = false;
  toast.textContent = message;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; toast.textContent = ""; }, 6000);
}

// Replace this browser's copy with a profile from the server and apply its defaults.
function useProfile(saved) {
  const next = profileFromServer(saved);
  const theme = next.defaults.theme;
  delete next.defaults.theme;
  const previous = profile.defaults;
  profile = next;
  try { localStorage.setItem(profileKey, JSON.stringify(profile)); } catch { /* Memory only. */ }
  if (theme && theme !== document.documentElement.dataset.theme) {
    applyTheme(theme);
    try { localStorage.setItem("midnight-munch-theme", theme); } catch { /* Theme works without storage. */ }
  }
  // Only settings that changed are applied, so the current start is kept unless the home station changed.
  for (const key of Object.keys(defaultFields)) {
    if (JSON.stringify(previous[key]) !== JSON.stringify(profile.defaults[key])) applyDefault(key);
  }
  if (profileDialog.open) {
    fillIdentity();
    renderDefaults();
  }
  renderProfile();
  renderOrigin();
  renderProfileSave();
}

function acceptSnapshot(data) {
  serverState = { known: true, exists: Boolean(data.exists), has_recovery_code: Boolean(data.has_recovery_code),
    updated_at: data.updated_at ?? null };
}

async function sendJson(method, path, body) {
  const response = await fetch(path, {
    method, credentials: "same-origin", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}), signal: AbortSignal.timeout(15000)
  });
  const data = await response.json().catch(() => ({}));
  return { status: response.status, ok: response.ok && !data.error, data };
}

const syncEnabled = () => sync.consent === true;
function scheduleSync(delay = 800) {
  if (!syncEnabled()) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(pushProfile, delay);
}

// Saves run one after another, so a recovery code request can wait for the save that sets the cookie.
function pushProfile() {
  clearTimeout(syncTimer);
  const run = syncChain.then(() => syncEnabled() ? sendProfile() : false);
  syncChain = run.catch(() => false);
  return run;
}

async function sendProfile() {
  let saved = false;
  try {
    const { status, ok, data } = await sendJson("PUT", "/api/profile",
      { ...profileForServer(), if_updated_at: sync.base ?? null });
    if (ok) {
      acceptSnapshot(data);
      setSync({ base: data.updated_at, dirty: false });
      setText("syncStatus", "Saved on this server.");
      saved = true;
    } else if (status === 409) {
      // The server copy changed on another device after this browser last synced: the newer one wins.
      acceptSnapshot({ exists: Boolean(data.profile), updated_at: data.updated_at, has_recovery_code: serverState.has_recovery_code });
      setSync({ base: data.updated_at ?? null, dirty: false });
      useProfile(data.profile);
      setText("syncStatus", "Updated from your other device.");
      showToast("Updated from your other device.");
    } else if (status === 429) {
      const wait = Math.min(60, Math.max(1, Number(data.retry_after) || 5));
      setText("syncStatus", "Saved on this browser. Saving on the server in a moment.");
      syncTimer = setTimeout(pushProfile, wait * 1000);
    } else {
      setText("syncStatus", `Saved on this browser only. The server said: ${data.error || "Try again."}`);
    }
  } catch {
    setText("syncStatus", "Saved on this browser. It will save on the server when you are back online.");
  } finally {
    renderProfileSave();
  }
  return saved;
}
window.addEventListener("online", () => { if (sync.dirty) scheduleSync(0); });

document.addEventListener("profile-changed", (event) => {
  setSync({ dirty: true });
  if (syncEnabled()) {
    setText("syncStatus", "Saving…");
    scheduleSync();
  } else if (sync.consent === undefined && serverState.known && !serverState.exists && byId("profileDialog").open) {
    // The server has no profile yet: upload this browser's copy only after the person agrees.
    byId("syncConsent").hidden = false;
  } else if (event.detail?.status && sync.consent === false) {
    setText("syncStatus", "Saved on this browser only.");
  }
  renderProfileSave();
});
byId("themeToggle").addEventListener("click", () => {
  if (syncEnabled()) {
    setSync({ dirty: true });
    scheduleSync();
  }
});
byId("syncConsentYes").addEventListener("click", async () => {
  setSync({ consent: true, dirty: true });
  byId("syncConsent").hidden = true;
  setText("syncStatus", "Saving…");
  byId("profileNextAction").focus();
  await pushProfile();
});
byId("syncConsentNo").addEventListener("click", () => {
  setSync({ consent: false });
  byId("syncConsent").hidden = true;
  setText("syncStatus", "Saved on this browser only.");
  byId("profileNextAction").focus();
  renderProfileSave();
});

async function loadServerProfile() {
  let data;
  try {
    data = await get("/api/profile");
  } catch {
    renderProfileSave();
    return;
  }
  acceptSnapshot(data);
  if (data.exists) {
    if (!localOk || !hadLocalProfile && sync.base !== data.updated_at) {
      // This browser lost its copy (or cannot keep one) while the server still has the profile.
      foundProfile = data.profile;
      offerNudge("found");
    } else if (sync.base === data.updated_at) {
      setSync({ consent: true });
      if (sync.dirty) scheduleSync(0);
    } else {
      const changed = !sameProfile(profile, profileFromServer(data.profile));
      const synced = Boolean(sync.base);
      setSync({ consent: true, base: data.updated_at, dirty: false });
      useProfile(data.profile);
      if (changed && synced) showToast("Updated from your other device.");
    }
  } else if (sync.base) {
    // The server copy was deleted: keep this browser's copy and ask again before uploading.
    setSync({ base: null, consent: undefined, dirty: true });
  } else if (syncEnabled() && sync.dirty) {
    scheduleSync(0);
  }
  renderProfileSave();
}

function restoreFoundProfile() {
  if (!foundProfile && !serverState.exists) return;
  setSync({ consent: true, base: serverState.updated_at, dirty: false });
  useProfile(foundProfile);
  foundProfile = null;
  showToast("Profile restored from this server.");
}
byId("useServerProfile").addEventListener("click", () => {
  restoreFoundProfile();
  byId("profileNextAction").focus();
});

document.addEventListener("server-data-deleted", () => {
  serverState = { known: true, exists: false, has_recovery_code: false, updated_at: null };
  setSync({ base: null, consent: undefined, dirty: true, code_saved_at: undefined });
  renderProfileSave();
});
document.addEventListener("browser-profile-reset", async () => {
  setSync({ base: null, consent: undefined, dirty: false, avatar_chosen: false });
  renderProfileSave();
  try {
    const data = await get("/api/profile");
    acceptSnapshot(data);
    if (data.exists) foundProfile = data.profile;
  } catch { /* The server copy can be loaded later from the profile screen. */ }
  renderProfileSave();
});
document.addEventListener("account-refreshed", () => {
  if (typeof account.profile_exists === "boolean") serverState.exists = account.profile_exists;
  if (typeof account.has_recovery_code === "boolean") serverState.has_recovery_code = account.has_recovery_code;
  renderProfileSave();
  offerNudge("protect");
});

// Completeness meter, next best action and recovery code status in the profile screen.
const shortDate = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
function profileSteps() {
  return [
    { done: Boolean(profile.name), label: "Add a name", step: 1 },
    { done: Boolean(sync.avatar_chosen) || profile.avatar !== freshProfile().avatar, label: "Pick an avatar", step: 1 },
    { done: Boolean(profile.defaults.station), label: "Set a home station", step: 2 },
    { done: serverState.exists && !sync.dirty, label: "Save it on this server", step: "server" },
    { done: serverState.has_recovery_code, label: "Save a recovery code", step: 3 }
  ];
}
function renderProfileSave() {
  const steps = profileSteps();
  const percent = Math.round(100 * steps.filter((item) => item.done).length / steps.length);
  setText("profileMeterLabel", `Profile ${percent} percent complete`);
  byId("profileMeter").value = percent;
  const next = steps.find((item) => !item.done);
  const action = byId("profileNextAction");
  action.hidden = !next;
  action.textContent = next ? `Next: ${next.label.toLowerCase()}` : "";
  action.dataset.step = next?.step ?? "";
  const saved = sync.code_saved_at && Number.isFinite(Date.parse(sync.code_saved_at))
    ? ` on ${shortDate.format(new Date(sync.code_saved_at))}` : "";
  setText("recoveryStatusLine", serverState.has_recovery_code ? `Recovery code saved${saved}` : "No recovery code yet");
  byId("recoveryAction").textContent = serverState.has_recovery_code ? "Make a new recovery code" : "Save a recovery code";
  byId("useServerProfile").hidden = !(serverState.exists && foundProfile !== null && sync.base !== serverState.updated_at);
  if (!byId("syncStatus").textContent) {
    setText("syncStatus", serverState.exists && sync.base === serverState.updated_at && !sync.dirty ? "Saved on this server."
      : sync.consent === false ? "Saved on this browser only." : "");
  }
}
byId("profileNextAction").addEventListener("click", (event) => {
  const step = event.currentTarget.dataset.step;
  if (step === "server") {
    if (sync.consent === true) pushProfile();
    else byId("syncConsent").hidden = false;
    byId("syncConsentYes").focus();
    return;
  }
  openOnboarding(Number(step) || 1, event.currentTarget);
});
byId("profileOpen").addEventListener("click", () => {
  byId("syncConsent").hidden = !(sync.consent === undefined && serverState.known && !serverState.exists && sync.dirty);
  renderProfileSave();
});
byId("createProfileOpen").addEventListener("click", (event) => openOnboarding(1, event.currentTarget));
byId("recoveryAction").addEventListener("click", (event) => openOnboarding(3, event.currentTarget));

// Onboarding: three skippable steps in one bottom sheet.
const onboarding = byId("onboardingSheet");
let onboardingStep = 1;
let onboardingTrigger;
let onboardStationId;
let avatarTouched = false;
let shownCode = "";

for (const [name, avatars] of Object.entries(avatarGroups)) {
  for (const [emoji, label] of avatars) {
    addChip(byId("onboardAvatars"), "onboard-avatar", emoji, emoji, "radio").setAttribute("aria-label", `${label} (${name})`);
  }
}

byId("onboardAvatars").addEventListener("change", () => { avatarTouched = true; });

function fillOnboardSelect(id, key) {
  const select = byId(id);
  select.replaceChildren(new Option("App default", ""));
  for (const [value, text] of profileChoices(key)) select.add(new Option(text, value));
  select.value = String(profile.defaults[key] ?? "");
  if (select.selectedIndex < 0) select.selectedIndex = 0;
}

function showOnboardingStep(step, focus = true) {
  onboardingStep = step;
  for (const number of [1, 2, 3]) byId(`onboardStep${number}`).hidden = number !== step;
  setText("onboardingProgress", `Step ${step} of 3`);
  byId("onboardBack").hidden = step === 1;
  const codeShown = Boolean(shownCode);
  byId("onboardSkip").hidden = step === 3 && codeShown;
  byId("onboardSkip").textContent = step === 3 ? "Skip for now" : "Skip";
  byId("onboardNext").textContent = step === 3 ? "Done" : "Next";
  byId("onboardNext").disabled = step === 3 && codeShown && !byId("savedRecoveryCode").checked;
  byId("onboardBack").disabled = step === 3 && codeShown;
  setText("onboardingStatus", "");
  if (step === 2) {
    fillOnboardSelect("onboardBudget", "budget");
    fillOnboardSelect("onboardStay", "stay");
    fillOnboardSelect("onboardWalk", "walk");
    const home = stations.get(profile.defaults.station);
    onboardStationId = home?.id;
    byId("onboardStation").value = home ? stationLabel(home) : "";
    closeOnboardStations();
  }
  if (step === 3) {
    byId("recoveryReplaceNote").hidden = !serverState.has_recovery_code || codeShown;
    byId("makeRecoveryCode").hidden = codeShown;
    byId("makeRecoveryCode").textContent = serverState.has_recovery_code ? "Make a new recovery code" : "Save a recovery code";
  }
  if (focus) byId(`onboardStep${step}Title`).focus();
}

function openOnboarding(step = 1, trigger = document.activeElement) {
  hideNudge();
  if (onboarding.open) {
    showOnboardingStep(step);
    return;
  }
  onboardingTrigger = trigger;
  byId("onboardName").value = profile.name;
  for (const input of byId("onboardAvatars").querySelectorAll("input")) input.checked = input.value === profile.avatar;
  avatarTouched = false;
  shownCode = "";
  byId("recoveryCodeBox").hidden = true;
  byId("recoveryCode").textContent = "";
  byId("savedRecoveryCode").checked = false;
  setText("recoveryStatus", "");
  onboarding.showModal();
  showOnboardingStep(step);
}

function closeOnboarding() {
  shownCode = "";
  byId("recoveryCode").textContent = "";
  byId("recoveryCodeBox").hidden = true;
  onboarding.close();
}
onboarding.addEventListener("cancel", (event) => {
  if (shownCode && !byId("savedRecoveryCode").checked) {
    event.preventDefault();
    setText("recoveryStatus", "Save your code first, then check “I saved my code somewhere safe”.");
    byId("savedRecoveryCode").focus();
    return;
  }
  event.preventDefault();
  closeOnboarding();
});
onboarding.addEventListener("close", () => {
  const target = onboardingTrigger?.isConnected && !onboardingTrigger.closest("[hidden]") ? onboardingTrigger
    : profileDialog.open ? byId("profileNextAction") : byId("profileOpen");
  renderProfileSave();
  target.focus();
});
byId("closeOnboarding").addEventListener("click", () => {
  if (shownCode && !byId("savedRecoveryCode").checked) {
    setText("recoveryStatus", "Save your code first, then check “I saved my code somewhere safe”.");
    byId("savedRecoveryCode").focus();
    return;
  }
  closeOnboarding();
});

function saveOnboardingStep(step) {
  if (step === 1) {
    profile.name = byId("onboardName").value.trim().slice(0, 28);
    const avatar = byId("onboardAvatars").querySelector("input:checked")?.value;
    if (avatar && avatarTouched) {
      profile.avatar = avatar;
      setSync({ avatar_chosen: true });
    }
  } else if (step === 2) {
    if (onboardStationId && stations.has(onboardStationId)) profile.defaults.station = onboardStationId;
    for (const [id, key] of [["onboardBudget", "budget"], ["onboardStay", "stay"], ["onboardWalk", "walk"]]) {
      if (byId(id).value) profile.defaults[key] = byId(id).value;
    }
    for (const key of ["station", "budget", "stay", "walk"]) applyDefault(key);
  } else return;
  // Creating a profile through this sheet is the consent to keep it on the server.
  setSync({ consent: true });
  saveProfile("onboardingStatus");
  renderProfile();
  renderOrigin();
}

byId("onboardNext").addEventListener("click", async () => {
  if (onboardingStep < 3) {
    saveOnboardingStep(onboardingStep);
    showOnboardingStep(onboardingStep + 1);
    return;
  }
  if (shownCode && !byId("savedRecoveryCode").checked) {
    setText("recoveryStatus", "Check “I saved my code somewhere safe” first.");
    byId("savedRecoveryCode").focus();
    return;
  }
  await pushProfile();
  closeOnboarding();
  showToast(serverState.has_recovery_code ? "Profile saved. Your recovery code is ready." : "Profile saved.");
});
byId("onboardSkip").addEventListener("click", async () => {
  if (onboardingStep < 3) {
    showOnboardingStep(onboardingStep + 1);
    return;
  }
  if (sync.consent === true && sync.dirty) await pushProfile();
  closeOnboarding();
});
byId("onboardBack").addEventListener("click", () => {
  if (onboardingStep > 1) showOnboardingStep(onboardingStep - 1);
});
byId("savedRecoveryCode").addEventListener("change", () => {
  byId("onboardNext").disabled = !byId("savedRecoveryCode").checked;
  setText("recoveryStatus", byId("savedRecoveryCode").checked ? "Thanks. Press Done to finish." : "");
});

// Home station combobox, using the station search ranking from station-picker.js.
let onboardMatches = [];
let onboardActive = -1;
function closeOnboardStations() {
  byId("onboardStationList").hidden = true;
  byId("onboardStationList").replaceChildren();
  byId("onboardStation").setAttribute("aria-expanded", "false");
  byId("onboardStation").removeAttribute("aria-activedescendant");
  onboardMatches = [];
  onboardActive = -1;
}
function renderOnboardStations() {
  const input = byId("onboardStation");
  const list = byId("onboardStationList");
  list.replaceChildren();
  onboardActive = -1;
  input.removeAttribute("aria-activedescendant");
  onboardMatches = input.value.trim() ? stationGroups(input.value)[0].matches.slice(0, 6).map(({ stop }) => stop) : [];
  for (const [index, stop] of onboardMatches.entries()) {
    const option = element("div", stationLabel(stop), list);
    option.id = `onboard-station-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => chooseOnboardStation(stop));
  }
  const open = onboardMatches.length > 0;
  list.hidden = !open;
  input.setAttribute("aria-expanded", String(open));
  setText("onboardStationAnnouncement", !input.value.trim() ? "" : !stations.size ? "Stations are still loading."
    : open ? `${onboardMatches.length} station${onboardMatches.length === 1 ? "" : "s"}, use up and down` : "No station matches.");
}
function highlightOnboardStation(index) {
  const options = [...byId("onboardStationList").children];
  onboardActive = index;
  for (const [position, option] of options.entries()) option.setAttribute("aria-selected", String(position === index));
  if (options[index]) {
    byId("onboardStation").setAttribute("aria-activedescendant", options[index].id);
    options[index].scrollIntoView({ block: "nearest" });
  }
}
function chooseOnboardStation(stop) {
  onboardStationId = stop.id;
  byId("onboardStation").value = stationLabel(stop);
  closeOnboardStations();
  setText("onboardStationAnnouncement", `${stationLabel(stop)} selected`);
}
byId("onboardStation").addEventListener("input", () => {
  onboardStationId = undefined;
  renderOnboardStations();
});
byId("onboardStation").addEventListener("blur", closeOnboardStations);
byId("onboardStation").addEventListener("keydown", (event) => {
  const count = onboardMatches.length;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!count) renderOnboardStations();
    if (!onboardMatches.length) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    highlightOnboardStation((onboardActive + step + onboardMatches.length) % onboardMatches.length);
  } else if (event.key === "Enter" && count) {
    event.preventDefault();
    chooseOnboardStation(onboardMatches[Math.max(0, onboardActive)]);
  } else if (event.key === "Escape" && count) {
    event.preventDefault();
    event.stopPropagation();
    closeOnboardStations();
  }
});

// The recovery code is shown once and kept only in this closure while the sheet is open.
async function requestRecoveryCode() {
  const button = byId("makeRecoveryCode");
  button.disabled = true;
  setText("recoveryStatus", "Making your code…");
  try {
    setSync({ consent: true });
    // A profile save first gives this browser its server cookie.
    if (!serverState.exists || sync.dirty) await pushProfile();
    let reply = await sendJson("POST", "/api/recovery/create", {});
    if (reply.status === 401) {
      await pushProfile();
      reply = await sendJson("POST", "/api/recovery/create", {});
    }
    if (!reply.ok || typeof reply.data.code !== "string") {
      setText("recoveryStatus", reply.data.error || "The code couldn't be made. Try again.");
      return;
    }
    shownCode = reply.data.code;
    serverState.has_recovery_code = true;
    setSync({ code_saved_at: reply.data.created_at ?? new Date().toISOString() });
    // Groups break only at the dashes on narrow screens.
    byId("recoveryCode").replaceChildren();
    for (const [index, group] of shownCode.split("-").entries()) {
      element("span", index ? `-${group}` : group, byId("recoveryCode"));
    }
    byId("recoveryCodeBox").hidden = false;
    button.hidden = true;
    byId("recoveryReplaceNote").hidden = true;
    byId("savedRecoveryCode").checked = false;
    showOnboardingStep(3, false);
    setText("recoveryStatus", "Your recovery code is ready. It is shown only once.");
    byId("recoveryCode").focus();
  } catch {
    setText("recoveryStatus", "The code couldn't be made. Check your connection and try again.");
  } finally {
    button.disabled = false;
    renderProfileSave();
  }
}
byId("makeRecoveryCode").addEventListener("click", requestRecoveryCode);
byId("copyRecoveryCode").addEventListener("click", async () => {
  if (!shownCode) return;
  try {
    await navigator.clipboard.writeText(shownCode);
    setText("recoveryStatus", "Copied.");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(byId("recoveryCode"));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    setText("recoveryStatus", "Copy didn't work here. The code is selected: copy it from your keyboard.");
  }
});
byId("downloadRecoveryCode").addEventListener("click", () => {
  if (!shownCode) return;
  const text = `Midnight Munch recovery code\n\n${shownCode}\n\n` +
    "This code is the only way to get your profile back on another phone. We can't recover it for you.\n";
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "midnight-munch-recovery-code.txt";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  setText("recoveryStatus", "Downloaded midnight-munch-recovery-code.txt.");
});
byId("printRecoveryCode").addEventListener("click", () => {
  if (!shownCode) return;
  document.documentElement.classList.add("printing-recovery");
  window.print();
  setTimeout(() => document.documentElement.classList.remove("printing-recovery"), 0);
});
window.addEventListener("afterprint", () => document.documentElement.classList.remove("printing-recovery"));

// Restore with a recovery code.
const restoreDialog = byId("restoreDialog");
let restoreConfirming = false;
const normalizedCode = (value) => value.toUpperCase().replace(/[\s-]+/g, "").replace(/[IL]/g, "1").replace(/O/g, "0");
function resetRestore() {
  restoreConfirming = false;
  byId("restoreConfirm").hidden = true;
  byId("restoreCancel").hidden = true;
  byId("restoreSubmit").textContent = "Restore";
  byId("restoreSubmit").disabled = false;
  byId("restoreCode").readOnly = false;
  setText("restoreError", "");
  setText("restoreStatus", "");
  byId("restoreCode").removeAttribute("aria-invalid");
}
function openRestore(trigger) {
  hideNudge();
  resetRestore();
  byId("restoreCode").value = "";
  openDialog(restoreDialog, trigger);
  byId("restoreCode").focus();
}
for (const id of ["restoreFromProfile", "restoreFromPrivacy", "nudgeRestore"]) {
  byId(id).addEventListener("click", (event) => openRestore(id === "nudgeRestore" ? byId("profileOpen") : event.currentTarget));
}
byId("restoreCode").addEventListener("input", () => {
  if (restoreConfirming) resetRestore();
  setText("restoreError", "");
  byId("restoreCode").removeAttribute("aria-invalid");
});
byId("restoreCancel").addEventListener("click", () => {
  resetRestore();
  byId("restoreCode").focus();
});
function restoreError(message) {
  resetRestore();
  setText("restoreError", message);
  byId("restoreCode").setAttribute("aria-invalid", "true");
  byId("restoreCode").focus();
}
byId("restoreForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = normalizedCode(byId("restoreCode").value);
  if (!code) {
    restoreError("Enter your recovery code.");
    return;
  }
  if (!/^[0-9A-Z]{20}$/.test(code)) {
    restoreError("A recovery code has 20 letters and numbers. Check it and try again.");
    return;
  }
  if (!restoreConfirming) {
    restoreConfirming = true;
    byId("restoreConfirm").hidden = false;
    byId("restoreCancel").hidden = false;
    byId("restoreSubmit").textContent = "Switch to restored profile";
    byId("restoreCode").readOnly = true;
    byId("restoreSubmit").focus();
    return;
  }
  byId("restoreSubmit").disabled = true;
  setText("restoreStatus", "Restoring…");
  try {
    const { ok, status, data } = await sendJson("POST", "/api/recovery/restore", { code });
    if (!ok) {
      restoreError(status === 429 ? data.error || "Too many tries. Wait a few minutes." : data.error || "That code didn't work.");
      return;
    }
    // The server switched this browser's cookie to the restored identity: reload everything.
    let snapshot = null;
    try { snapshot = await get("/api/profile"); } catch { /* The restore response is enough. */ }
    if (snapshot) acceptSnapshot(snapshot);
    else acceptSnapshot({ exists: Boolean(data.profile), has_recovery_code: true, updated_at: null });
    setSync({ consent: true, base: serverState.updated_at, dirty: false, code_saved_at: undefined });
    foundProfile = null;
    useProfile(data.profile);
    byId("restoreCode").value = "";
    resetRestore();
    restoreDialog.close();
    showToast("Profile restored. Your stamps and points are back.");
    refreshAccount();
    schedulePlaces();
    scheduleLateStrip();
  } catch {
    restoreError("Couldn't reach the server. Check your connection and try again.");
  }
});
restoreDialog.addEventListener("close", () => {
  byId("restoreCode").value = "";
  resetRestore();
});

// Nudges: never modal, at most one per session and one per 3 days.
const nudgeCopy = {
  plan: { text: "Save your start and budget? Create a profile in 20 seconds.", action: "Create profile", step: 1, restore: true },
  stamp: { text: "Nice, your first stamp! Add a name so your stamps and points are yours.", action: "Add a name", step: 1 },
  protect: { text: "Protect your progress: save a recovery code in case you switch phones.", action: "Save a code", step: 3 },
  welcome: { text: "Welcome back! Set a home station so planning starts in one tap.", action: "Set home station", step: 2, restore: true },
  found: { text: "We found your profile on this server. Restore it?", action: "Restore" }
};
const nudgeEligible = {
  plan: () => !serverState.exists && !profile.name,
  stamp: () => !profile.name && (account.visits.length > 0 || account.stamps.length > 0),
  protect: () => !serverState.has_recovery_code && ((account.points?.total ?? 0) >= 20 || (account.stats?.visits ?? account.visits.length) >= 3),
  welcome: () => !profile.defaults.station && (readStored(nudgeKey).days ?? []).length >= 3,
  found: () => serverState.exists && sync.base !== serverState.updated_at
};
let currentNudge;
let pendingNudge;

const nudgeBlocked = () => !byId("nightLoading").hidden || Boolean(pinnedTrip) || Boolean(pickRequest) ||
  Boolean(document.querySelector("dialog[open]"));

function offerNudge(id) {
  const state = readStored(nudgeKey);
  if (state.stopped || currentNudge || state.done?.[id] || !nudgeEligible[id]()) return;
  if (readStored(nudgeSessionKey, true).shown || memoryStore.get("nudge-shown")) return;
  if (Date.now() - (Number(state.last) || 0) < nudgeCooldown) return;
  if ((Number(state.snoozed?.[id]) || 0) > Date.now()) return;
  if (nudgeBlocked()) {
    pendingNudge = pendingNudge ?? id;
    return;
  }
  pendingNudge = undefined;
  currentNudge = id;
  memoryStore.set("nudge-shown", true);
  writeStored(nudgeSessionKey, { shown: id }, true);
  writeStored(nudgeKey, { ...state, last: Date.now() });
  const copy = nudgeCopy[id];
  setText("nudgeText", copy.text);
  setText("nudgePrimary", copy.action);
  byId("nudgeRestore").hidden = !copy.restore;
  byId("profileNudge").dataset.nudge = id;
  byId("profileNudge").hidden = false;
  // A polite announcement; focus stays where the person is.
  setText("nudgeAnnouncement", "");
  requestAnimationFrame(() => setText("nudgeAnnouncement", `Suggestion: ${copy.text}`));
}
function hideNudge(restoreFocus = false) {
  if (!currentNudge) return;
  const hadFocus = byId("profileNudge").contains(document.activeElement);
  currentNudge = undefined;
  byId("profileNudge").hidden = true;
  setText("nudgeAnnouncement", "");
  if (restoreFocus || hadFocus) {
    const heading = ["nightHeading", "pinnedHeading", "missedHeading", "planHeading"].map(byId)
      .find((node) => node && !node.closest("[hidden]"));
    (heading ?? byId("profileOpen")).focus({ preventScroll: true });
  }
}
function retryPendingNudge() {
  if (pendingNudge && !nudgeBlocked()) {
    const id = pendingNudge;
    pendingNudge = undefined;
    offerNudge(id);
  }
}
byId("nudgeLater").addEventListener("click", () => {
  const state = readStored(nudgeKey);
  writeStored(nudgeKey, { ...state, snoozed: { ...state.snoozed, [currentNudge]: Date.now() + nudgeSnooze } });
  hideNudge(true);
});
byId("nudgeStop").addEventListener("click", () => {
  writeStored(nudgeKey, { ...readStored(nudgeKey), stopped: true });
  hideNudge(true);
});
byId("nudgePrimary").addEventListener("click", () => {
  const id = currentNudge;
  const state = readStored(nudgeKey);
  writeStored(nudgeKey, { ...state, done: { ...state.done, [id]: true } });
  hideNudge();
  if (id === "found") {
    restoreFoundProfile();
    byId("profileOpen").focus();
  } else openOnboarding(nudgeCopy[id].step, byId("profileOpen"));
});
// Hide the card while planning, following a pinned trip or using a dialog; show a waiting nudge later.
const nudgeWatcher = new MutationObserver(() => {
  if (nudgeBlocked()) hideNudge();
  else retryPendingNudge();
});
for (const id of ["nightLoading", "pinnedScreen", "tripBar"]) nudgeWatcher.observe(byId(id), { attributes: true, attributeFilter: ["hidden"] });
for (const dialog of document.querySelectorAll("dialog")) nudgeWatcher.observe(dialog, { attributes: true, attributeFilter: ["open"] });

document.addEventListener("night-planned", () => offerNudge("plan"));
document.addEventListener("community-checkin", () => offerNudge("stamp"));

// Visit days for the "welcome back" nudge (local dates, at most the last 10).
(() => {
  const state = readStored(nudgeKey);
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
  const days = Array.isArray(state.days) ? state.days.filter((day) => typeof day === "string") : [];
  if (!days.includes(today)) writeStored(nudgeKey, { ...state, days: [...days, today].slice(-10) });
})();

renderProfileSave();
loadServerProfile().then(() => {
  if (!currentNudge) offerNudge("welcome");
});
