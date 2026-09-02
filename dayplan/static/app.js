"use strict";

const el = (id) => document.getElementById(id);
const list = el("task-list");
const newList = el("new-list");

const state = {
  tasks: [],
  fresh: [],
  current: null,
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
      if (body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_) { /* keep the status line */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function relativeTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

const PRIORITY_LABEL = { 3: "high", 2: "med", 1: "low" };

function badge(cls, text) {
  const node = document.createElement("span");
  node.className = cls;
  node.textContent = text;
  return node;
}

function dueBadge(task) {
  if (!task.due) return null;
  const days = task.due_in_days;
  if (days !== null && days < 0) return badge("badge due-over", `${task.due} · ${-days}d late`);
  if (days === 0) return badge("badge due-today", "today");
  if (days === 1) return badge("badge", "tomorrow");
  return badge("badge", task.due);
}

/* --------------------------------------------------------------------- toggl
 * The Toggl Track extension ships a generic "DOM Integration" that turns any
 * .toggl-root element into a real Toggl button, reading the data-* attributes
 * below. So we only render the slot; the extension supplies the button, using
 * its own session. No API token and no timer logic on our side.
 *
 * If the extension is absent or the domain is not mapped to DOM Integration in
 * its settings, the slot stays empty and `.toggl-slot:empty` collapses it.
 */
// One slot element, reused across renders. render() rebuilds the whole list on
// every load, and a fresh .toggl-root each time means the extension has to
// notice and re-process it every single time — a race we do not need. Keeping
// the node lets us only invalidate it when the featured task actually changes.
let togglNode = null;
let togglNodeTaskId = null;

function togglSlot(task) {
  if (!togglNode) {
    togglNode = document.createElement("span");
    togglNode.className = "toggl-slot toggl-root";
  }
  if (togglNodeTaskId !== task.id) {
    // Different task: drop the button the extension built and the `toggl`
    // marker it sets, so its `.toggl-root:not(.toggl)` selector matches again.
    togglNode.replaceChildren();
    togglNode.classList.remove("toggl");
    togglNodeTaskId = task.id;
  }
  togglNode.dataset.description = task.title;
  // The numeric Toggl project id, resolved during sync from the project map.
  // Deliberately no data-project-name: an unmapped task should land in Toggl
  // with no project rather than inventing one from the source's own naming.
  if (task.toggl_project_id) togglNode.dataset.projectId = String(task.toggl_project_id);
  else delete togglNode.dataset.projectId;
  const tags = [task.source, ...(task.tags || [])].filter(Boolean);
  if (tags.length) togglNode.dataset.tags = tags.join(",");
  else delete togglNode.dataset.tags;
  togglNode.dataset.className = "dayplan";
  return togglNode;
}

/* --------------------------------------------------------------------- cards */

function titleNode(task, tag) {
  const node = document.createElement(tag);
  if (task.url) {
    const link = document.createElement("a");
    link.href = task.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.draggable = false;
    link.textContent = task.title;
    node.appendChild(link);
  } else {
    node.textContent = task.title;
  }
  return node;
}

function metaNodes(task, into) {
  into.appendChild(badge("badge ref", `#${task.ref}`));
  into.appendChild(badge("badge source", task.source));
  const due = dueBadge(task);
  if (due) into.appendChild(due);
  if (task.priority > 0) into.appendChild(badge("badge prio", PRIORITY_LABEL[task.priority]));
  if (task.project) into.appendChild(badge("badge", task.project));
  for (const tag of task.tags || []) into.appendChild(badge("badge", `#${tag}`));
}

function buildCard(task, rank, isFeatured = false) {
  const card = document.createElement("div");
  card.className = `card src-${task.source}`;
  if (!task.pinned) card.classList.add("loose");
  if (isFeatured) card.classList.add("featured");
  card.dataset.id = task.id;
  card.draggable = true;

  if (isFeatured) {
    const eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "Working on now";
    card.appendChild(eyebrow);
  }

  const idx = document.createElement("div");
  idx.className = "idx";
  idx.textContent = String(rank);
  card.appendChild(idx);

  const title = titleNode(task, isFeatured ? "h1" : "div");
  title.className = "title";
  card.appendChild(title);

  const side = document.createElement("div");
  side.className = "side";
  if (task.zone === "new") {
    const accept = document.createElement("button");
    accept.className = "iconbtn accept";
    accept.textContent = "→";
    accept.title = "Keep: move into the list";
    accept.addEventListener("click", async () => {
      try {
        await api("/api/accept", {
          method: "POST",
          body: JSON.stringify({ task_id: task.id }),
        });
        await load();
      } catch (error) {
        toast(error.message, true);
      }
    });
    side.appendChild(accept);
  }
  // The Toggl button belongs only to the task being worked on.
  if (isFeatured) side.appendChild(togglSlot(task));
  if (task.pinned) {
    const unpin = document.createElement("button");
    unpin.className = "iconbtn";
    unpin.textContent = "✕";
    unpin.title = "Unpin: back to default order";
    unpin.addEventListener("click", async () => {
      try {
        await api(`/api/order/${encodeURIComponent(task.id)}`, { method: "DELETE" });
        await load();
      } catch (error) {
        toast(error.message, true);
      }
    });
    side.appendChild(unpin);
  }
  card.appendChild(side);

  const meta = document.createElement("div");
  meta.className = "meta";
  metaNodes(task, meta);
  card.appendChild(meta);

  return card;
}

function renderChips() {
  const chips = el("source-chips");
  chips.replaceChildren();
  const sources = new Set([...state.tasks, ...state.fresh].map((t) => t.source));
  for (const source of [...sources].sort()) {
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

function matchesFilter(task) {
  if (state.hiddenSources.has(task.source)) return false;
  const needle = state.filterText.trim().toLowerCase();
  if (!needle) return true;
  const hay = `${task.title} ${task.project || ""} ${(task.tags || []).join(" ")}`;
  return hay.toLowerCase().includes(needle);
}

function render() {
  const tasks = state.tasks.filter(matchesFilter);
  const fresh = state.fresh.filter(matchesFilter);

  list.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.tasks.length ? "Nothing matches." : "Nothing in the list yet.";
    list.appendChild(empty);
  } else {
    let dividerDone = !tasks.some((t) => t.pinned);
    tasks.forEach((task, index) => {
      if (!dividerDone && !task.pinned) {
        const divider = document.createElement("div");
        divider.className = "divider";
        divider.textContent = "unordered";
        list.appendChild(divider);
        dividerDone = true;
      }
      // Position 1 is the task being worked on: same list, bigger card.
      list.appendChild(buildCard(task, index + 1, index === 0));
    });
  }

  newList.replaceChildren();
  if (!fresh.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.fresh.length ? "Nothing matches." : "Nothing new.";
    newList.appendChild(empty);
  } else {
    fresh.forEach((task) => newList.appendChild(buildCard(task, "", false)));
  }

  renderChips();
  renderIntegrations();
  el("list-count").textContent =
    tasks.length === state.tasks.length
      ? `${tasks.length}`
      : `${tasks.length} / ${state.tasks.length}`;
  el("new-count").textContent =
    fresh.length === state.fresh.length
      ? `${fresh.length}`
      : `${fresh.length} / ${state.fresh.length}`;

  const everything = [...state.tasks, ...state.fresh];
  const overdue = everything.filter((t) => t.overdue).length;
  const dueToday = everything.filter((t) => t.due_in_days === 0).length;
  const stats = el("stats");
  stats.replaceChildren();
  const add = (label, value, bad) => {
    const span = document.createElement("span");
    if (bad) span.className = "bad";
    span.append(document.createTextNode(`${label} `));
    const b = document.createElement("b");
    b.textContent = value;
    span.appendChild(b);
    stats.appendChild(span);
  };
  add("overdue", overdue, overdue > 0);
  add("due today", dueToday, false);
}

/* --------------------------------------------------------------------- data */

async function load() {
  try {
    const data = await api("/api/state");
    state.tasks = data.tasks;
    state.fresh = data.new || [];
    state.current = data.current;
    state.integrations = data.integrations || [];
    render();
  } catch (error) {
    toast(`Could not load: ${error.message}`, true);
  }
}

async function commitOrder(draggedId) {
  // Pin the prefix of the list down to whichever is lower: the card just
  // dropped, or the last already-pinned card. Pinning the whole list on every
  // drag would make everything pinned after one move and the unordered tail
  // would never come back.
  const displayed = [...list.querySelectorAll(".card")].map((card) => card.dataset.id);
  const byId = new Map(state.tasks.map((task) => [task.id, task]));
  const draggedIndex = displayed.indexOf(draggedId);
  let lastPinned = -1;
  displayed.forEach((id, index) => {
    if (byId.get(id)?.pinned) lastPinned = index;
  });
  const cut = Math.max(draggedIndex, lastPinned);
  if (cut < 0) return;
  await api("/api/order", {
    method: "PUT",
    body: JSON.stringify({ ids: displayed.slice(0, cut + 1) }),
  });
}

/* ---------------------------------------------------------- drag and drop */

function cardAfterPoint(container, y) {
  for (const card of container.querySelectorAll(".card:not(.dragging)")) {
    const box = card.getBoundingClientRect();
    if (y < box.top + box.height / 2) return card;
  }
  return null;
}

function wireDropTarget(container) {
  container.addEventListener("dragover", (event) => {
    if (!state.dragId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    container.classList.add("dropping");
    const dragged = document.querySelector(`.card[data-id="${CSS.escape(state.dragId)}"]`);
    if (!dragged) return;
    const placeholder = container.querySelector(".empty");
    if (placeholder) placeholder.remove();
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

wireDropTarget(list);
wireDropTarget(newList);

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
  list.classList.remove("dropping");
  newList.classList.remove("dropping");
  if (card) card.classList.remove("dragging");
  if (!draggedId) return;

  // Cancelled drag (Escape, or dropped outside a list): undo the preview.
  if (!state.dropped) {
    render();
    return;
  }
  state.dropped = false;

  const wasNew = state.fresh.some((task) => task.id === draggedId);
  const nowInNew = newList.contains(card);

  try {
    if (nowInNew) {
      // Dragged into the new pile: untriage it. Ordering the pile is
      // meaningless, so a new-to-new drag is a no-op.
      if (!wasNew) {
        await api("/api/dismiss", {
          method: "POST",
          body: JSON.stringify({ task_id: draggedId }),
        });
      } else {
        render();
        return;
      }
    } else {
      // Landing in the main list pins the prefix, which also accepts a new
      // task in one move: it comes out of the pile with a real position.
      await commitOrder(draggedId);
    }
    await load();
  } catch (error) {
    toast(error.message, true);
    await load();
  }
});

/* ------------------------------------------------------- integration status */

const STATE_TEXT = { ok: "ok", empty: "no tasks", error: "error", never: "never synced", off: "off" };

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
    [relativeTime(a.finished_at), a.ok ? "ok" : "err", a.trigger || "?", a.fetched, a.added, a.updated, a.closed]
      .forEach((value, index) => {
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
  const state_ = document.createElement("span");
  state_.className = `state s-${integ.state}`;
  state_.textContent = STATE_TEXT[integ.state] || integ.state;
  title.append(dot, name, state_);
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
  } else if (integ.state === "error") {
    message.className = "integ-msg error";
    message.textContent = integ.last_error || "The last sync failed.";
  } else if (integ.state === "empty") {
    message.className = "integ-msg warn";
    message.textContent =
      "Synced without error but the provider returned 0 tasks. The credentials are fine — " +
      "look at the filters instead: Asana projects and sections, the Jira JQL, or the " +
      "TickTick due window.";
  } else if (integ.state === "never") {
    message.className = "integ-msg warn";
    message.textContent = "Configured but never synced yet. Hit Sync.";
  }
  if (message.className) card.appendChild(message);

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

const closeDrawer = () => { el("drawer").hidden = true; };

/* ----------------------------------------------------------------- controls */

el("sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    const report = await api("/api/sync", { method: "POST", body: JSON.stringify({}) });
    const total = (bucket) => Object.values(bucket || {}).reduce((a, b) => a + b, 0);
    const parts = [];
    if (total(report.added)) parts.push(`${total(report.added)} new`);
    if (total(report.closed)) parts.push(`${total(report.closed)} closed`);
    if (total(report.updated)) parts.push(`${total(report.updated)} updated`);
    if ((report.skipped || []).length) parts.push(`skipped: ${report.skipped.join(", ")}`);
    const errors = Object.entries(report.errors || {});
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

let searchTimer;
el("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.filterText = event.target.value;
    render();
  }, 120);
});

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea")) return;
  if (event.key === "/") { event.preventDefault(); el("search").focus(); }
  if (event.key === "s") el("sync").click();
  if (event.key === "i") openDrawer();
  if (event.key === "Escape") closeDrawer();
});

load();
