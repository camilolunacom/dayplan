"use strict";

const el = (id) => document.getElementById(id);
const list = el("task-list");
const newList = el("new-list");
const laterList = el("later-list");

const state = {
  tasks: [],
  fresh: [],
  current: null,
  integrations: [],
  filterText: "",
  hiddenSources: new Set(),
  dragId: null,
  dropped: false,
  orderRevision: null,
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
function togglSlot(task) {
  // Each card needs its own DOM Integration root so every task can be timed.
  const togglNode = document.createElement("span");
  togglNode.className = "toggl-slot toggl-root";
  togglNode.dataset.description = task.title;
  // Only the NAME does anything. The extension's resolve-project handler takes
  // { projectName, selectedWorkspaceId } and nothing else; data-project-id is
  // read by its dom-integration script and then dropped, so we do not send it.
  // A task whose Toggl project name is not unique carries no name at all: it
  // tracks unprojected instead of against an arbitrary same-named project.
  if (task.toggl_project) togglNode.dataset.projectName = task.toggl_project;
  else delete togglNode.dataset.projectName;
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

  if (isFeatured) {
    const eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "Working on now";
    card.appendChild(eyebrow);
  }

  // The handle is the only thing that starts a drag, so it has to be a real
  // control: full card height and a finger-sized target. Anywhere else on the
  // card stays scrollable, which is why `touch-action: none` lives here only.
  const handle = document.createElement("button");
  handle.type = "button";
  handle.className = "idx grip";
  handle.setAttribute("aria-label", rank ? `Reorder, position ${rank}` : "Move into the list");
  handle.title = rank ? "Drag to reorder" : "Drag into the list to keep";
  // Clicks are meaningless on a drag handle and would submit nothing; stop
  // them so a stray tap does not look like it did something.
  handle.addEventListener("click", (event) => event.preventDefault());

  const dots = document.createElement("span");
  dots.className = "gripdots";
  dots.textContent = "⠿";
  handle.appendChild(dots);
  card.appendChild(handle);

  const title = titleNode(task, isFeatured ? "h1" : "div");
  title.className = "title";
  card.appendChild(title);

  const side = document.createElement("div");
  side.className = "side";
  // Keep Toggl before the task action so accept/unpin stays at the far right.
  side.appendChild(togglSlot(task));
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
  const tasks = state.tasks.filter((task) => task.pinned && matchesFilter(task));
  const later = state.tasks.filter((task) => !task.pinned && matchesFilter(task));
  const fresh = state.fresh.filter(matchesFilter);

  list.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.tasks.length ? "Nothing matches." : "Nothing in the list yet.";
    list.appendChild(empty);
  } else {
    tasks.forEach((task, index) => {
      // Position 1 is the task being worked on.
      list.appendChild(buildCard(task, index + 1, index === 0));
    });
  }

  laterList.replaceChildren();
  if (!later.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.tasks.some((t) => !t.pinned) ? "Nothing matches." : "Nothing for later.";
    laterList.appendChild(empty);
  } else {
    later.forEach((task) => laterList.appendChild(buildCard(task, "", false)));
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
  const laterTotal = state.tasks.filter((task) => !task.pinned).length;
  el("later-count").textContent = later.length === laterTotal ? `${later.length}` : `${later.length} / ${laterTotal}`;

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
/* The backend syncs on a 15 minute schedule of its own, so a tab left open all
   day would otherwise sit on whatever it fetched at breakfast. So we poll, but
   only where it can possibly matter: a visible tab, no drag in progress, one
   request at a time, and no rebuild of the DOM unless something actually
   changed. */

const POLL_MS = 60000;

let pollTimer = null;
let inFlight = null;
let lastSignature = null;
let catchUpPending = false;

// Everything the UI draws out of /api/state, and nothing else. Integration
// timestamps and attempt history move on every scheduled sync even when it
// found nothing, and the drawer refetches those itself when it opens, so they
// stay out: only the tasks and a visible integration status count as a change.
function stateSignature(data) {
  return JSON.stringify([
    data.tasks,
    data.new || [],
    data.current,
    (data.integrations || []).map((integ) => [integ.source, integ.state, integ.open_count]),
  ]);
}

async function refresh(background) {
  try {
    const data = await api("/api/state");
    // A drag started while the request was in the air. The list belongs to the
    // finger until it lets go; drop the payload, the next poll brings it back.
    if (background && drag.card) return;

    const signature = stateSignature(data);
    const changed = signature !== lastSignature;
    lastSignature = signature;

    state.tasks = data.tasks;
    state.fresh = data.new || [];
    state.current = data.current;
    state.integrations = data.integrations || [];
    state.orderRevision = data.order_revision;

    // A poll that brings back exactly what is already on screen must not
    // rebuild it: render() replaces every card, which throws away the Toggl
    // button the extension built and any scroll position with it. A load asked
    // for by hand always draws, because the action that triggered it moved
    // cards around itself.
    if (background && !changed) return;
    render();
  } catch (error) {
    // A failed poll is not news anybody asked for, and a toast every minute
    // while the network is down is worse than a list that stopped updating.
    if (!background) toast(`Could not load: ${error.message}`, true);
  }
}

// One /api/state request at a time. Two in flight can finish out of order and
// leave the older payload on screen, so a poll that lands while a load is
// already running is dropped -- that load brings fresh state anyway -- while a
// load an action asked for queues behind it, and so reads the state that
// follows its own write.
function load({ background = false } = {}) {
  if (inFlight && background) return inFlight;
  const run = () => {
    // Whatever this request brings back is newer than anything a queued
    // catch-up would have asked for, so it settles that debt too.
    catchUpPending = false;
    return refresh(background);
  };
  const chained = inFlight ? inFlight.then(run, run) : run();
  const tracked = chained.then(() => {
    if (inFlight === tracked) inFlight = null;
    if (catchUpPending) catchUp();
  });
  inFlight = tracked;
  return tracked;
}

// Coming back into view is not a beat of the interval, it is the one refresh
// that shows what the scheduled syncs did while the tab was away -- so when a
// drag or an open request is in the way it waits its turn instead of being
// dropped. Whoever clears the way runs it: endDrag, or the request in flight.
function catchUp() {
  if (document.visibilityState !== "visible") {
    // The next visibilitychange asks again; a queued refresh must not outlive
    // the tab going away, or it would fire against a hidden tab.
    catchUpPending = false;
    return;
  }
  if (drag.card || inFlight) {
    catchUpPending = true;
    return;
  }
  catchUpPending = false;
  load({ background: true });
}

function pollNow() {
  if (document.visibilityState !== "visible" || drag.card) return;
  load({ background: true });
}

function startPolling() {
  if (pollTimer !== null || document.visibilityState !== "visible") return;
  pollTimer = setInterval(pollNow, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") {
    stopPolling();
    return;
  }
  // Back on screen: show whatever the scheduled syncs did while it was away
  // instead of sitting out the rest of the interval.
  startPolling();
  catchUp();
});

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
    body: JSON.stringify({ ids: displayed.slice(0, cut + 1), revision: state.orderRevision }),
  });
}

/* ---------------------------------------------------------- drag and drop */
/* Pointer Events rather than HTML5 drag-and-drop: the native API never fires
   on touch, so the tablet could not reorder at all. One code path now covers
   mouse, touch and stylus. */

const drag = {
  id: null,
  card: null,
  pointerId: null,
  from: null,
  moved: false,
  scrollTimer: null,
};

const CONTAINERS = () => [list, newList, laterList];

function containerUnder(x, y) {
  for (const container of CONTAINERS()) {
    const box = container.getBoundingClientRect();
    if (x >= box.left && x <= box.right && y >= box.top - 40 && y <= box.bottom + 40) {
      return container;
    }
  }
  return null;
}

function cardAfterPoint(container, y) {
  for (const card of container.querySelectorAll(".card")) {
    if (card === drag.card) continue;
    const box = card.getBoundingClientRect();
    if (y < box.top + box.height / 2) return card;
  }
  return null;
}

/* Dragging to a position off screen is otherwise impossible on a tablet: the
   finger holding the card cannot also scroll. This is scripted scrolling, so
   it keeps working while `touch-action: none` blocks browser panning. */
function autoScroll(container, y) {
  // Whichever element actually scrolls: the list itself on desktop, the page
  // when the layout is stacked and the lists are content-height.
  const scroller =
    container.scrollHeight > container.clientHeight
      ? container
      : document.querySelector("main");

  // Measure the edges against the *scroller's* visible box, not the
  // container's. Stacked, a list is far taller than the screen, so its own
  // edges sit off screen and a finger at the bottom of the viewport would
  // never look "near an edge".
  const box = scroller.getBoundingClientRect();
  const edge = 72;
  let delta = 0;
  if (y < box.top + edge) delta = -Math.ceil((box.top + edge - y) / 5);
  else if (y > box.bottom - edge) delta = Math.ceil((y - (box.bottom - edge)) / 5);
  if (delta) scroller.scrollTop += delta;
}

function startDrag(event) {
  const grip = event.target.closest?.(".grip");
  if (!grip || event.button > 0) return;
  const card = grip.closest(".card");
  if (!card) return;

  drag.id = card.dataset.id;
  drag.card = card;
  drag.pointerId = event.pointerId;
  drag.from = card.parentElement;
  drag.moved = false;
  card.classList.add("dragging");
  // Belt and braces for touch: `touch-action: none` on the handle stops the
  // gesture being read as a pan, but a finger that slips off the handle
  // mid-drag would otherwise start scrolling the page. Lock panning globally
  // until the drag ends. Our own auto-scroll is scripted, so it still works.
  document.body.classList.add("dragging");
  grip.setPointerCapture(event.pointerId);
  event.preventDefault();
}

function moveDrag(event) {
  if (drag.pointerId !== event.pointerId || !drag.card) return;
  event.preventDefault();
  drag.moved = true;

  const container = containerUnder(event.clientX, event.clientY) || drag.card.parentElement;
  container.classList.add("dropping");
  for (const other of CONTAINERS()) {
    if (other !== container) other.classList.remove("dropping");
  }
  const placeholder = container.querySelector(".empty");
  if (placeholder) placeholder.remove();

  const reference = cardAfterPoint(container, event.clientY);
  if (reference) container.insertBefore(drag.card, reference);
  else container.appendChild(drag.card);

  autoScroll(container, event.clientY);
}

async function endDrag(event, cancelled = false) {
  if (drag.pointerId !== event.pointerId || !drag.card) return;
  const { id, card, from, moved } = drag;
  drag.id = drag.card = drag.pointerId = drag.from = null;
  card.classList.remove("dragging");
  document.body.classList.remove("dragging");
  for (const container of CONTAINERS()) container.classList.remove("dropping");

  try {
    // A tap on the grip is not a reorder.
    if (cancelled || !moved) {
      if (cancelled) render();
      return;
    }

    const nowInNew = newList.contains(card);
    const nowInLater = laterList.contains(card);
    const wasNew = from === newList;
    const wasLater = from === laterList;

    try {
      if (nowInNew) {
        if (wasNew) {
          render(); // ordering the new pile means nothing
          return;
        }
        await api("/api/dismiss", { method: "POST", body: JSON.stringify({ task_id: id }) });
      } else if (nowInLater) {
        if (wasLater) {
          render();
          return;
        }
        await api(`/api/order/${encodeURIComponent(id)}`, { method: "DELETE" });
      } else {
        // Landing in the list pins the prefix, which also accepts a new task in
        // one move: it comes out of the pile with a real position.
        await commitOrder(id);
      }
      await load();
    } catch (error) {
      toast(error.message, true);
      await load();
    }
  } finally {
    // The finger is off the list, so a refresh it was holding off can run. On
    // the paths that reloaded already this is a no-op: that load took the debt.
    if (catchUpPending) catchUp();
  }
}

document.addEventListener("pointerdown", startDrag);
document.addEventListener("pointermove", moveDrag, { passive: false });
document.addEventListener("pointerup", (event) => endDrag(event));
document.addEventListener("pointercancel", (event) => endDrag(event, true));

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
startPolling();
