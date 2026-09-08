// The backend syncs on its own every 15 minutes, so a tab left open has to go
// and look: these cover the polling contract in dayplan/static/app.js.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, samplePayload } from "./harness.mjs";

const MINUTE = 60_000;

test("the first load happens once, on startup", async () => {
  const app = await loadApp();
  assert.equal(app.stateRequests(), 1);
  assert.equal(app.renders(), 1);
});

test("polls /api/state every 60 seconds while the tab is visible", async () => {
  const app = await loadApp();

  await app.tick(MINUTE);
  assert.equal(app.stateRequests(), 2, "one poll after a minute");

  await app.tick(MINUTE);
  assert.equal(app.stateRequests(), 3, "and another the minute after");
});

test("does not poll while the tab is hidden", async () => {
  const app = await loadApp();
  await app.setVisibility("hidden");

  await app.tick(5 * MINUTE);
  assert.equal(app.stateRequests(), 1, "a hidden tab asks for nothing");
});

test("refreshes immediately when the tab becomes visible again", async () => {
  const app = await loadApp();
  await app.setVisibility("hidden");
  await app.tick(5 * MINUTE);
  const before = app.stateRequests();

  await app.setVisibility("visible");
  assert.equal(app.stateRequests(), before + 1, "catches up at once, without waiting a minute");

  await app.tick(MINUTE);
  assert.equal(app.stateRequests(), before + 2, "and the interval is running again");
});

test("never refreshes while a drag is in progress", async () => {
  const app = await loadApp();
  app.startDrag();
  const before = app.stateRequests();

  await app.tick(3 * MINUTE);
  assert.equal(app.stateRequests(), before, "a poll would yank the list out from under the finger");

  await app.endDrag();
  await app.tick(MINUTE);
  assert.equal(app.stateRequests(), before + 1, "polling resumes once the drag is over");
});

test("a load in flight is never overlapped by a poll", async () => {
  const app = await loadApp();
  const release = app.hold();

  await app.tick(MINUTE);
  const during = app.stateRequests();
  await app.tick(3 * MINUTE);
  assert.equal(app.stateRequests(), during, "no second request while one is still open");

  await release();
});

test("an unchanged payload does not rerender the list", async () => {
  const app = await loadApp();
  const before = app.renders();

  await app.tick(MINUTE);
  assert.equal(app.renders(), before, "nothing changed server side, so nothing to redraw");
});

test("integration bookkeeping alone does not rerender the list", async () => {
  const app = await loadApp();
  const before = app.renders();

  // What a scheduled sync that found nothing new leaves behind: fresh
  // timestamps and one more attempt in the history, same tasks, same status.
  const next = samplePayload();
  next.integrations[0].last_attempt_at = "2026-09-08T10:15:00Z";
  next.integrations[0].last_success_at = "2026-09-08T10:15:00Z";
  next.integrations[0].history = [{ finished_at: "2026-09-08T10:15:00Z", ok: true, trigger: "schedule" }];
  app.serve(next);

  await app.tick(MINUTE);
  assert.equal(app.renders(), before, "clock movement is not a change");
});

test("a scheduled sync that adds a task shows up without touching anything", async () => {
  const app = await loadApp();
  const before = app.renders();

  const next = samplePayload();
  next.new.push({ ...next.new[0], id: "d", ref: "d", title: "Arrived while I was reading" });
  app.serve(next);

  await app.tick(MINUTE);
  assert.equal(app.renders(), before + 1, "the new arrival is drawn");
});

test("a scheduled sync that changes integration status shows up", async () => {
  const app = await loadApp();
  const before = app.renders();

  const next = samplePayload();
  next.integrations[0].state = "error";
  next.integrations[0].last_error = "401 from TickTick";
  app.serve(next);

  await app.tick(MINUTE);
  assert.equal(app.renders(), before + 1, "the status pill has to turn red");
});

test("manual Sync still reloads and redraws", async () => {
  const app = await loadApp();
  const beforeRenders = app.renders();
  const beforeRequests = app.stateRequests();

  app.document.getElementById("sync").dispatch("click", {
    currentTarget: app.document.getElementById("sync"),
  });
  await app.flush();

  assert.ok(
    app.fetches.some((call) => call.path === "/api/sync" && call.method === "POST"),
    "Sync posts to /api/sync"
  );
  assert.equal(app.stateRequests(), beforeRequests + 1, "and reloads the state after");
  assert.equal(app.renders(), beforeRenders + 1, "a load asked for by hand always redraws");
});

test("a reorder still commits the order and reloads", async () => {
  const app = await loadApp();
  const beforeRequests = app.stateRequests();

  app.startDrag(1);
  await app.movePointer(10); // above the first card
  await app.endDrag();

  const put = app.fetches.find((call) => call.path === "/api/order" && call.method === "PUT");
  assert.ok(put, "the new order is sent");
  assert.deepEqual(JSON.parse(put.options.body).ids, ["b", "a"], "with the card moved to the top");
  assert.equal(app.stateRequests(), beforeRequests + 1, "and the list is reloaded after");
});

test("becoming visible during a drag waits for the finger to let go", async () => {
  const app = await loadApp();
  app.startDrag();
  const before = app.stateRequests();

  // Switching away and back mid-drag: the tablet does this every time a
  // notification steals the screen with a card still under the thumb.
  await app.setVisibility("hidden");
  await app.setVisibility("visible");
  assert.equal(app.stateRequests(), before, "the list belongs to the finger, visible or not");

  await app.endDrag();
  assert.equal(app.stateRequests(), before + 1, "and the catch-up lands the moment it lets go");

  await app.flush();
  assert.equal(app.stateRequests(), before + 1, "exactly once, not once per blocked attempt");
});

test("becoming visible while a load is in flight still catches up", async () => {
  const app = await loadApp();
  const release = app.hold();

  await app.tick(MINUTE); // a poll goes out and stays open
  const during = app.stateRequests();

  await app.setVisibility("hidden");
  await app.setVisibility("visible");
  assert.equal(app.stateRequests(), during, "nothing is stacked on top of the open request");

  await release();
  assert.equal(app.stateRequests(), during + 1, "the catch-up runs once that request settles");

  await app.flush();
  assert.equal(app.stateRequests(), during + 1, "and only the one");
});

test("a reorder's own reload doubles as the catch-up the drag blocked", async () => {
  const app = await loadApp();
  const before = app.stateRequests();

  app.startDrag(1);
  await app.setVisibility("hidden");
  await app.setVisibility("visible");
  await app.movePointer(10);
  await app.endDrag();

  assert.equal(app.stateRequests(), before + 1, "one reload after the drop, not two");
});
