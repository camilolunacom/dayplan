// Test harness for dayplan/static/app.js.
//
// app.js is a plain browser script, so there is nothing to import: the harness
// runs its source in a vm context whose globals are stubs we can drive and
// inspect. No jsdom, no bundler, no dependency of any kind -- `node --test`
// ships with the runtime.
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const APP_JS = join(dirname(fileURLToPath(import.meta.url)), "..", "dayplan", "static", "app.js");

/* Enough of the clock to step 60 seconds without waiting 60 seconds. */
function makeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();

  const clock = {
    setTimeout(fn, ms = 0) {
      timers.set(++seq, { fn, at: now + ms, every: null });
      return seq;
    },
    setInterval(fn, ms = 0) {
      timers.set(++seq, { fn, at: now + ms, every: ms });
      return seq;
    },
    clearTimeout(id) { timers.delete(id); },
    clearInterval(id) { timers.delete(id); },

    async flush() {
      for (let i = 0; i < 50; i += 1) await Promise.resolve();
    },

    async tick(ms) {
      const target = now + ms;
      for (;;) {
        let dueId = null;
        let due = null;
        for (const [id, timer] of timers) {
          if (timer.at <= target && (due === null || timer.at < due.at)) {
            dueId = id;
            due = timer;
          }
        }
        if (due === null) break;
        now = due.at;
        if (due.every === null) timers.delete(dueId);
        else due.at = now + due.every;
        due.fn();
        await clock.flush();
      }
      now = target;
      await clock.flush();
    },

    pending: () => timers.size,
  };
  return clock;
}

/* A DOM node, shallow but honest about the bits app.js actually touches. */
function makeElement(tag = "div", id = null) {
  const classes = new Set();
  const node = {
    tagName: tag.toUpperCase(),
    id,
    children: [],
    dataset: {},
    style: {},
    hidden: false,
    disabled: false,
    textContent: "",
    listeners: new Map(),
    replaceChildrenCalls: 0,

    get className() { return [...classes].join(" "); },
    set className(value) {
      classes.clear();
      for (const part of String(value).split(/\s+/).filter(Boolean)) classes.add(part);
    },
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name);
        else classes.delete(name);
        return on;
      },
    },

    replaceChildren(...kids) {
      node.replaceChildrenCalls += 1;
      node.children = [];
      kids.forEach((kid) => node.appendChild(kid));
    },
    appendChild(kid) {
      if (kid?.parentElement) kid.parentElement.removeChild(kid);
      node.children.push(kid);
      if (kid) kid.parentElement = node;
      return kid;
    },
    append(...kids) { kids.forEach((kid) => node.appendChild(kid)); },
    insertBefore(kid, reference) {
      if (kid?.parentElement) kid.parentElement.removeChild(kid);
      const at = node.children.indexOf(reference);
      node.children.splice(at < 0 ? node.children.length : at, 0, kid);
      if (kid) kid.parentElement = node;
      return kid;
    },
    removeChild(kid) {
      const at = node.children.indexOf(kid);
      if (at >= 0) node.children.splice(at, 1);
      if (kid) kid.parentElement = null;
      return kid;
    },
    remove() { node.parentElement?.removeChild(node); },
    contains(other) {
      for (let walk = other; walk; walk = walk.parentElement) if (walk === node) return true;
      return false;
    },

    querySelectorAll(selector) { return descendants(node).filter((kid) => matches(kid, selector)); },
    querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
    closest(selector) {
      for (let walk = node; walk; walk = walk.parentElement) if (matches(walk, selector)) return walk;
      return null;
    },
    matches(selector) { return matches(node, selector); },

    setAttribute(name, value) { node[name] = value; },
    getBoundingClientRect: () => ({ top: 0, bottom: 100, left: 0, right: 100, height: 100, width: 100 }),
    scrollHeight: 100,
    clientHeight: 100,
    scrollTop: 0,
    setPointerCapture() {},

    addEventListener(type, handler) {
      if (!node.listeners.has(type)) node.listeners.set(type, []);
      node.listeners.get(type).push(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of node.listeners.get(type) || []) handler({ preventDefault() {}, ...event });
    },
    parentElement: null,
  };
  return node;
}

function descendants(node) {
  return node.children.flatMap((kid) => [kid, ...descendants(kid)]);
}

function matches(node, selector) {
  if (!node || !selector) return false;
  return selector
    .split(",")
    .map((part) => part.trim())
    .some((part) => {
      if (part.startsWith(".")) return node.classList.contains(part.slice(1));
      if (part.startsWith("#")) return node.id === part.slice(1);
      return node.tagName === part.toUpperCase();
    });
}

/* A /api/state payload that renders: one pinned task in the list, one arrival. */
export function samplePayload() {
  const task = (id, extra = {}) => ({
    id,
    ref: id,
    source: "ticktick",
    title: `Task ${id}`,
    url: null,
    due: null,
    due_in_days: null,
    overdue: false,
    priority: 0,
    project: null,
    tags: [],
    pinned: true,
    zone: "list",
    toggl_project: null,
    ...extra,
  });
  return {
    today: "2026-09-08",
    current: task("a"),
    tasks: [task("a"), task("b", { pinned: false })],
    new: [task("c", { zone: "new", pinned: false })],
    summary: {},
    sources: ["ticktick"],
    integrations: [
      {
        source: "ticktick",
        state: "ok",
        open_count: 3,
        last_fetched: 3,
        last_attempt_at: "2026-09-08T10:00:00Z",
        last_success_at: "2026-09-08T10:00:00Z",
        last_error: null,
        history: [],
      },
    ],
  };
}

export async function loadApp({ visible = true, payload = samplePayload() } = {}) {
  const clock = makeClock();
  const elements = new Map();
  const fetches = [];
  let held = null; // queued /api/state responses, when the test holds them open
  let current = payload;

  const document = {
    visibilityState: visible ? "visible" : "hidden",
    body: makeElement("body"),
    listeners: new Map(),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement("div", id));
      return elements.get(id);
    },
    createElement: (tag) => makeElement(tag),
    querySelector: (selector) => (selector === "main" ? document.getElementById("main") : null),
    addEventListener(type, handler) {
      if (!document.listeners.has(type)) document.listeners.set(type, []);
      document.listeners.get(type).push(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of document.listeners.get(type) || []) handler({ preventDefault() {}, ...event });
    },
  };

  const respond = (body) => ({
    ok: true,
    status: 200,
    json: () => Promise.resolve(JSON.parse(JSON.stringify(body))),
  });

  const bodyFor = (path) => {
    if (path.startsWith("/api/state")) return current;
    if (path.startsWith("/api/integrations")) return current.integrations;
    if (path.startsWith("/api/sync")) return { added: {}, closed: {}, updated: {}, skipped: [], errors: {} };
    return {};
  };

  function fetchStub(path, options = {}) {
    fetches.push({ path, method: options.method || "GET", options });
    if (path.startsWith("/api/state") && held) {
      return new Promise((resolve) => held.push(() => resolve(respond(bodyFor(path)))));
    }
    return Promise.resolve(respond(bodyFor(path)));
  }

  const sandbox = {
    document,
    fetch: fetchStub,
    console,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval,
    clearInterval: clock.clearInterval,
    location: { href: "http://localhost/" },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const context = createContext(sandbox);
  runInContext(readFileSync(APP_JS, "utf8"), context, { filename: "app.js" });
  await clock.flush();

  const list = document.getElementById("task-list");

  return {
    clock,
    document,
    fetches,
    tick: clock.tick,
    flush: clock.flush,

    stateRequests: () => fetches.filter((call) => call.path.startsWith("/api/state")).length,
    renders: () => list.replaceChildrenCalls,

    /** Replace what /api/state answers with from now on. */
    serve(next) { current = next; },
    payload: () => current,

    /** Hold /api/state responses open; the returned function releases them. */
    hold() {
      held = [];
      return () => {
        const queued = held;
        held = null;
        queued.forEach((release) => release());
        return clock.flush();
      };
    },

    async setVisibility(value) {
      document.visibilityState = value;
      document.dispatch("visibilitychange");
      await clock.flush();
    },

    cards: () => list.querySelectorAll(".card"),

    /** Press on a rendered card's grip, the way a real reorder starts. */
    startDrag(index = 1, pointerId = 1) {
      const card = list.querySelectorAll(".card")[index];
      const grip = card.querySelector(".grip");
      document.dispatch("pointerdown", { target: grip, pointerId, button: 0 });
      return card;
    },

    async movePointer(clientY = 10, pointerId = 1) {
      document.dispatch("pointermove", { pointerId, clientX: 50, clientY });
      await clock.flush();
    },

    async endDrag(pointerId = 1) {
      document.dispatch("pointerup", { pointerId });
      await clock.flush();
    },
  };
}
