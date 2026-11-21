const form = document.querySelector("#preferences");
const station = form.elements.station;
const status = document.querySelector("#status");
const result = document.querySelector("#result");
const reroll = document.querySelector("#reroll");
const dishHeading = document.querySelector("#dish");
const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

station.value = "Boston University Central";

// Budget slider moves in $3 steps (3..36), matching price_band in the database.
const budget = form.elements.budget;
const budgetValue = document.querySelector("#budgetValue");
function showBudget() {
  budgetValue.textContent = `$${budget.value}`;
  budget.setAttribute("aria-valuetext", `${budget.value} dollars`);
}
budget.addEventListener("input", showBudget);
showBudget();
let fixture;
let previousId;

function matchesCraving(dish, craving) {
  return craving === "cheap" ? dish.price <= 8 : dish.tags.includes(craving);
}

async function pick(preferences) {
  if (!fixture) {
    const response = await fetch("./fixtures/dishes.json");
    if (!response.ok) throw new Error("Fixture unavailable");
    fixture = (await response.json()).dishes;
  }

  const { budget, dietary, cravings, line, station, excludeId } = preferences;
  const eligible = fixture.filter(dish => dish.price <= budget &&
    dietary.every(need => dish.dietary.includes(need)));

  if (!eligible.length) return { message: "No demo dishes meet your budget and dietary needs." };

  const alternatives = eligible.filter(dish => dish.id !== excludeId);
  if (!alternatives.length) return { message: "There’s no other dish that meets your budget and dietary needs." };

  let candidates = alternatives.filter(dish => dish.line === line);
  const notes = [];
  if (!candidates.length) {
    candidates = alternatives;
    notes.push("Relaxed the starting-line preference. This dish may require a transfer.");
  }

  const ranked = candidates.map(dish => ({
    dish,
    matches: cravings.filter(craving => matchesCraving(dish, craving)).length
  }));
  const best = Math.max(...ranked.map(entry => entry.matches));
  const top = ranked.filter(entry => entry.matches === best);
  const weight = ({ dish }) => dish.stop === station ? 3 : 1;
  let ticket = Math.random() * top.reduce((sum, entry) => sum + weight(entry), 0);
  const chosen = top.find(entry => {
    ticket -= weight(entry);
    return ticket < 0;
  }).dish;
  const missed = cravings.filter(craving => !matchesCraving(chosen, craving));
  if (missed.length) notes.push(`Relaxed craving preferences for ${missed.join(", ")}.`);
  return { dish: chosen, message: notes.join(" ") };
}

async function spin(isReroll) {
  if (!form.reportValidity()) return;
  const data = new FormData(form);
  const submit = form.querySelector("button");
  submit.disabled = reroll.disabled = true;
  status.textContent = "";
  if (!isReroll) result.hidden = true;

  try {
    const selection = station.selectedOptions[0];
    const { dish, message } = await pick({
      budget: Number(data.get("budget")),
      dietary: data.getAll("dietary"),
      cravings: data.getAll("craving"),
      station: selection.value,
      line: selection.parentElement.label.replace(" Line", ""),
      excludeId: isReroll ? previousId : undefined
    });
    status.textContent = message;
    if (!dish) return;

    previousId = dish.id;
    dishHeading.textContent = dish.name;
    document.querySelector("#price").textContent = dollars.format(dish.price);
    document.querySelector("#restaurant").textContent = dish.restaurant;
    const lineLabel = document.querySelector("#line");
    lineLabel.textContent = `${dish.line} Line`;
    lineLabel.className = dish.line.toLowerCase();
    const walk = `walk ${dish.walkMinutes} min`;
    document.querySelector("#directions").textContent = dish.stop === selection.value
      ? `From ${dish.stop}, ${walk}.`
      : `Get off at ${dish.stop}, then ${walk}.`;
    result.hidden = false;
    dishHeading.focus();
  } catch {
    status.textContent = "Couldn’t load the demo dishes. Try spinning again.";
  } finally {
    submit.disabled = reroll.disabled = false;
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  spin(false);
});
reroll.addEventListener("click", () => spin(true));
