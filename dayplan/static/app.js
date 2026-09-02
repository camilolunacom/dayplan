"use strict";

const el = (id) => document.getElementById(id);

const planList = el("plan-list");
const pendingList = el("pending-list");

const state = {
  day: null,
  today: null,
  plan: [],
  pending: [],
  sources: [],
  filterText: "",
  hiddenSources: new Set(),
  dragId: null,
  dropped: false,
};

/* ------------------------------------------------------------------ helpers */

function toast(message, isError = false) {
  const node = el("toast");
  node.textContent = message;
  node.classList.toggle("err", isError);
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), isError ? 6000 : 2500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* keep the status line */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function fmtMinutes(total) {
  if (!total) return "0m";
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (hours && minutes) return `${hours}h ${minutes}m`;
  return hours ? `${hours}h` : `${minutes}m`;
}

function shiftDay(iso, days) {
  const date = new Date(`${iso}T12:00:00`);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

const PRIORITY_LABEL = { 3: "high", 2: "med", 1: "low" };

function dueBadge(task) {
  if (!task.due) return null;
  const days = task.due_in_days;
  let cls = "badge";
  let label = task.due;
  if (days !== null && days < 0) {
    cls += " due-over";
    label = `${task.due} (${-days}d late)`;
  } else if (days === 0) {
    cls += " due-today";
    label = `${task.due} (today)`;
  } else if (days !== null && days === 1) {
    label = `${task.due} (tomorrow)`;
  }
  return { cls, label };
}

/* ------------------------------------------------------------------ rendering */

function badge(cls, text) {
  const node = document.createElement("span");
  node.className = cls;
  node.textContent = text;
  return node;
}

function buildCard(task, index, isNext = false) {
  const card = document.createElement("div");
  card.className = `card src-${task.source}`;
  card.dataset.id = task.id;
  card.draggable = true;
  if (task.done) card.classList.add("is-done");
  if (isNext) card.classList.add("is-next");

  const inPlan = index !== null;

  const idx = document.createElement("div");
  idx.className = "idx";
  idx.textContent = inPlan ? String(index + 1) : "";
  card.appendChild(idx);

  const title = document.createElement("div");
  title.className = "title";
  if (task.url) {
    const link = document.createElement("a");
    link.href = task.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.draggable = false;
    link.textContent = task.title;
    title.appendChild(link);
  } else {
    title.textContent = task.title;
  }
  card.appendChild(title);

  const side = document.createElement("div");
  side.className = "side";
  if (inPlan) {
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = task.done;
    check.title = "Done (local only, does not push back)";
    check.addEventListener("change", () => patchPlan(task.id, { done: check.checked }));
    side.appendChild(check);

    const est = document.createElement("input");
    est.type = "number";
    est.className = "est";
    est.min = "0";
    est.step = "5";
    est.placeholder = "min";
    est.title = "Estimate in minutes";
    est.value = task.est_minutes ?? "";
    est.addEventListener("change", () => {
      patchPlan(task.id, { est_minutes: Number(est.value) || 0 });
    });
    side.appendChild(est);

    const remove = document.createElement("button");
    remove.className = "iconbtn";
    remove.textContent = "✕";
    remove.title = "Back to pending";
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/plan/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
        await load();
      } catch (error) {
        toast(error.message, true);
      }
    });
    side.appendChild(remove);
  }
  card.appendChild(side);

  const meta = document.createElement("div");
  meta.className = "meta";
  if (isNext) meta.appendChild(badge("badge next", "next"));
  meta.appendChild(badge("badge ref", `#${task.ref}`));
  meta.appendChild(badge(`badge src-${task.source}`, task.source));
  const due = dueBadge(task);
  if (due) meta.appendChild(badge(due.cls, due.label));
  if (task.priority > 0) meta.appendChild(badge("badge prio", PRIORITY_LABEL[task.priority]));
  if (task.project) meta.appendChild(badge("badge", task.project));
  for (const tag of task.tags || []) meta.appendChild(badge("badge", `#${tag}`));
  card.appendChild(meta);

  if (inPlan) {
    const noteWrap = document.createElement("div");
    noteWrap.className = "note";
    const note = document.createElement("textarea");
    note.className = "notefield";
    note.rows = 1;
    note.placeholder = "note…";
    note.value = task.plan_note || "";
    note.addEventListener("change", () => patchPlan(task.id, { note: note.value }));
    noteWrap.appendChild(note);
    card.appendChild(noteWrap);
  }

  return card;
}

function nextTask(plan) {
  return plan.find((task) => !task.done) || null;
}

function visiblePending() {
  const needle = state.filterText.trim().toLowerCase();
  return state.pending.filter((task) => {
    if (state.hiddenSources.has(task.source)) return false;
    if (!needle) return true;
    const haystack = `${task.title} ${task.project || ""} ${(task.tags || []).join(" ")}`.toLowerCase();
    return haystack.includes(needle);
  });
}

function renderList(container, tasks, numbered) {
  container.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = numbered ? "Nothing planned yet." : "Nothing pending.";
    container.appendChild(empty);
    return;
  }
  const upcoming = numbered ? nextTask(tasks) : null;
  tasks.forEach((task, index) => {
    const isNext = Boolean(upcoming && task.id === upcoming.id);
    container.appendChild(buildCard(task, numbered ? index : null, isNext));
  });
}

function renderChips() {
  const chips = el("source-chips");
  chips.replaceChildren();
  const present = [...new Set(state.pending.map((t) => t.source))].sort();
  for (const source of present) {
    const chip = document.createElement("button");
    chip.className = `chip${state.hiddenSources.has(source) ? "" : " on"}`;
    chip.textContent = source;
    chip.addEventListener("click", () => {
      if (state.hiddenSources.has(source)) state.hiddenSources.delete(source);
      else state.hiddenSources.add(source);
      render();
    });
    chips.appendChild(chip);
  }
}

function render() {
  const pending = visiblePending();
  renderList(pendingList, pending, false);
  renderList(planList, state.plan, true);

  el("pending-count").textContent = `${pending.length}${
    pending.length === state.pending.length ? "" : ` / ${state.pending.length}`
  }`;
  el("plan-count").textContent = String(state.plan.length);
  renderChips();

  const open = state.plan.filter((t) => !t.done);
  const estimated = open.reduce((sum, t) => sum + (t.est_minutes || 0), 0);
  el("plan-meta").textContent = `${open.length} open · ${state.plan.length - open.length} done · ${fmtMinutes(estimated)} estimated`;

  const upcoming = nextTask(state.plan);
  const nextLine = el("next-line");
  nextLine.replaceChildren();
  if (upcoming) {
    const label = document.createElement("b");
    label.textContent = "Next:";
    const title = document.createElement("span");
    title.className = "t";
    title.textContent = upcoming.title;
    nextLine.append(label, title);
  } else if (state.plan.length) {
    nextLine.textContent = "All done for this day.";
  }

  const overdue = state.pending.filter((t) => t.overdue).length;
  const dueToday = state.pending.filter((t) => t.due_in_days === 0).length;
  const stats = el("stats");
  stats.replaceChildren();
  const add = (label, value, bad) => {
    const span = document.createElement("span");
    if (bad) span.className = "bad";
    span.innerHTML = `${label} <b>${value}</b>`;
    stats.appendChild(span);
  };
  add("pending", state.pending.length, false);
  add("overdue", overdue, overdue > 0);
  add("due today", dueToday, false);
  el("day-input").value = state.day;
}

/* ------------------------------------------------------------------ data */

async function load() {
  try {
    const data = await api(`/api/state?day=${encodeURIComponent(state.day || "today")}`);
    state.day = data.day;
    state.today = data.today;
    state.plan = data.plan;
    state.pending = data.pending;
    state.sources = data.sources;
    render();
  } catch (error) {
    toast(`Could not load: ${error.message}`, true);
  }
}

async function patchPlan(taskId, payload) {
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/plan`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    await load();
  } catch (error) {
    toast(error.message, true);
  }
}

async function commitPlanOrder() {
  const ids = [...planList.querySelectorAll(".card")].map((card) => card.dataset.id);
  await api(`/api/plan/${encodeURIComponent(state.day)}/order`, {
    method: "PUT",
    body: JSON.stringify({ ids }),
  });
}

/* ------------------------------------------------------------------ drag and drop */

function cardAfterPoint(container, y) {
  const cards = [...container.querySelectorAll(".card:not(.dragging)")];
  for (const card of cards) {
    const box = card.getBoundingClientRect();
    if (y < box.top + box.height / 2) return card;
  }
  return null;
}

function wireDragTarget(container) {
  container.addEventListener("dragover", (event) => {
    if (!state.dragId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    container.classList.add("dropping");

    const dragged = document.querySelector(`.card[data-id="${CSS.escape(state.dragId)}"]`);
    if (!dragged) return;
    const empty = container.querySelector(".empty");
    if (empty) empty.remove();
    const reference = cardAfterPoint(container, event.clientY);
    if (reference) container.insertBefore(dragged, reference);
    else container.appendChild(dragged);
  });

  container.addEventListener("dragleave", (event) => {
    if (!container.contains(event.relatedTarget)) container.classList.remove("dropping");
  });

  container.addEventListener("drop", (event) => {
    event.preventDefault();
    container.classList.remove("dropping");
    state.dropped = true;
  });
}

document.addEventListener("dragstart", (event) => {
  const card = event.target.closest?.(".card");
  if (!card) return;
  state.dragId = card.dataset.id;
  state.dropped = false;
  card.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", card.dataset.id);
});

document.addEventListener("dragend", async (event) => {
  const card = event.target.closest?.(".card");
  const draggedId = state.dragId;
  state.dragId = null;
  planList.classList.remove("dropping");
  pendingList.classList.remove("dropping");
  if (card) card.classList.remove("dragging");
  if (!draggedId) return;

  // Cancelled drag (Escape, or dropped outside a list): undo the live preview.
  if (!state.dropped) {
    render();
    return;
  }
  state.dropped = false;

  const nowInPlan = planList.contains(card);
  const wasInPlan = state.plan.some((task) => task.id === draggedId);

  try {
    if (nowInPlan) {
      await commitPlanOrder();
    } else if (wasInPlan) {
      await api(`/api/plan/tasks/${encodeURIComponent(draggedId)}`, { method: "DELETE" });
    } else {
      render(); // pending -> pending, nothing to persist
      return;
    }
    await load();
  } catch (error) {
    toast(error.message, true);
    await load();
  }
});

wireDragTarget(planList);
wireDragTarget(pendingList);

/* ------------------------------------------------------------------ controls */

el("sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    const report = await api("/api/sync", { method: "POST", body: JSON.stringify({}) });
    const parts = [];
    const total = (bucket) => Object.values(bucket || {}).reduce((a, b) => a + b, 0);
    if (total(report.added)) parts.push(`${total(report.added)} new`);
    if (total(report.closed)) parts.push(`${total(report.closed)} closed`);
    if (total(report.updated)) parts.push(`${total(report.updated)} updated`);
    const errors = Object.entries(report.errors || {});
    const skipped = report.skipped || [];
    if (skipped.length) parts.push(`skipped: ${skipped.join(", ")}`);
    let message = parts.length ? parts.join(" · ") : "nothing changed";
    if (errors.length) message += `\n${errors.map(([k, v]) => `${k}: ${v}`).join("\n")}`;
    toast(message, errors.length > 0);
    await load();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Sync";
  }
});

el("prev-day").addEventListener("click", () => { state.day = shiftDay(state.day, -1); load(); });
el("next-day").addEventListener("click", () => { state.day = shiftDay(state.day, 1); load(); });
el("go-today").addEventListener("click", () => { state.day = state.today; load(); });
el("day-input").addEventListener("change", (event) => {
  if (event.target.value) { state.day = event.target.value; load(); }
});

let searchTimer;
el("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.filterText = event.target.value; render(); }, 120);
});

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea")) return;
  if (event.key === "/") { event.preventDefault(); el("search").focus(); }
  if (event.key === "s") el("sync").click();
  if (event.key === "t") el("go-today").click();
});

load();
