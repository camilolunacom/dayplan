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
  integrations: [],
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
  renderIntegrations();
}

/* --------------------------------------------------- integration status */

const STATE_TEXT = {
  ok: "ok",
  empty: "no tasks",
  error: "error",
  never: "never synced",
  off: "off",
};

function relativeTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  // Clamp: the container clock and the browser clock need not agree, and a
  // few seconds of skew should not render as "-7s ago".
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function renderIntegrations() {
  const host = el("integrations");
  host.replaceChildren();
  for (const integ of state.integrations) {
    const pill = document.createElement("button");
    pill.className = `integ state-${integ.state}`;
    pill.title = "Integration status";
    const dot = document.createElement("span");
    dot.className = `dot s-${integ.state}`;
    const label = document.createElement("span");
    label.textContent =
      integ.state === "off" ? integ.source : `${integ.source} ${integ.open_count}`;
    pill.append(dot, label);
    pill.addEventListener("click", openDrawer);
    host.appendChild(pill);
  }
}

function kv(pairs) {
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [key, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  }
  return dl;
}

function attemptsTable(history) {
  if (!history || !history.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "attempts";
  const table = document.createElement("table");
  const head = document.createElement("tr");
  for (const h of ["when", "", "trigger", "got", "new", "upd", "closed"]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const a of history) {
    const tr = document.createElement("tr");
    const cells = [
      relativeTime(a.finished_at),
      a.ok ? "ok" : "err",
      a.trigger || "?",
      a.fetched,
      a.added,
      a.updated,
      a.closed,
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 1) td.className = a.ok ? "ok" : "bad";
      tr.appendChild(td);
    });
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  return wrap;
}

function integCard(integ) {
  const card = document.createElement("div");
  card.className = "integ-card";

  const title = document.createElement("h4");
  const dot = document.createElement("span");
  dot.className = `dot s-${integ.state}`;
  const name = document.createElement("span");
  name.textContent = integ.source;
  const badge = document.createElement("span");
  badge.className = `state s-${integ.state}`;
  badge.textContent = STATE_TEXT[integ.state] || integ.state;
  title.append(dot, name, badge);
  card.appendChild(title);

  card.appendChild(
    kv([
      ["open tasks", integ.open_count],
      ["returned last run", integ.last_fetched],
      ["last attempt", integ.last_attempt_at ? relativeTime(integ.last_attempt_at) : "never"],
      ["last success", integ.last_success_at ? relativeTime(integ.last_success_at) : "never"],
    ])
  );

  const message = document.createElement("div");
  if (integ.state === "off") {
    message.className = "integ-msg info";
    message.textContent = integ.detail || "Not configured.";
    card.appendChild(message);
  } else if (integ.state === "error") {
    message.className = "integ-msg error";
    message.textContent = integ.last_error || "The last sync failed.";
    card.appendChild(message);
  } else if (integ.state === "empty") {
    message.className = "integ-msg warn";
    message.textContent =
      "Synced without error but the provider returned 0 tasks. The credentials are " +
      "fine — look at the filters instead: Asana workspaces, the Jira JQL, or which " +
      "TickTick projects are visible.";
    card.appendChild(message);
  } else if (integ.state === "never") {
    message.className = "integ-msg warn";
    message.textContent = "Configured but never synced yet. Hit Sync.";
    card.appendChild(message);
  }

  const table = attemptsTable(integ.history);
  if (table) card.appendChild(table);
  return card;
}

async function openDrawer() {
  const drawer = el("drawer");
  const body = el("drawer-body");
  drawer.hidden = false;
  body.replaceChildren();
  try {
    const rows = await api("/api/integrations?history=8");
    state.integrations = rows;
    for (const integ of rows) body.appendChild(integCard(integ));
    renderIntegrations();
  } catch (error) {
    const message = document.createElement("div");
    message.className = "integ-msg error";
    message.textContent = error.message;
    body.appendChild(message);
  }
}

function closeDrawer() {
  el("drawer").hidden = true;
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
    state.integrations = data.integrations || [];
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

el("drawer-close").addEventListener("click", closeDrawer);
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
  if (event.key === "i") openDrawer();
  if (event.key === "Escape") closeDrawer();
});

load();
