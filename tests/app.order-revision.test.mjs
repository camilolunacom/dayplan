// The order revision is how the backend rejects a drag that started against
// state that changed underneath it (e.g. a task confirmed-reopened and
// dropped its old plan row). These cover the frontend half of that contract.
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, samplePayload } from "./harness.mjs";

test("a reorder sends the order revision it last loaded", async () => {
  const payload = samplePayload();
  payload.order_revision = 7;
  const app = await loadApp({ payload });

  app.startDrag(1);
  await app.movePointer(10);
  await app.endDrag();

  const put = app.fetches.find((call) => call.path === "/api/order" && call.method === "PUT");
  assert.ok(put, "the new order is sent");
  const body = JSON.parse(put.options.body);
  assert.deepEqual(body.ids, ["b", "a"]);
  assert.equal(body.revision, 7, "the revision travels with the order");
});

test("a stale order revision (409) still reloads current state", async () => {
  const app = await loadApp();
  const beforeRequests = app.stateRequests();

  app.respondOnce("PUT", "/api/order", 409, { detail: "order revision is stale" });

  app.startDrag(1);
  await app.movePointer(10);
  await app.endDrag();

  assert.equal(
    app.stateRequests(),
    beforeRequests + 1,
    "the existing error path reloads current state on conflict"
  );
});

test("409 recovery redraws the authoritative order and the next drag sends its revision", async () => {
  const app = await loadApp();

  // What the server actually holds by the time the rejected write lands:
  // a different order, at a higher revision than the one the drag used.
  const authoritative = samplePayload();
  authoritative.order_revision = 9;
  authoritative.tasks = [authoritative.tasks[1], authoritative.tasks[0]];
  app.serve(authoritative);

  app.respondOnce("PUT", "/api/order", 409, { detail: "order revision is stale" });

  app.startDrag(1);
  await app.movePointer(10);
  await app.endDrag();

  assert.deepEqual(
    app.cards().map((card) => card.dataset.id),
    ["b", "a"],
    "the list must redraw back to the authoritative order, not the rejected drag"
  );

  // A subsequent, ordinary drag must use the revision that reload just loaded.
  app.startDrag(1);
  await app.movePointer(10);
  await app.endDrag();

  const puts = app.fetches.filter((call) => call.path === "/api/order" && call.method === "PUT");
  const retry = puts[puts.length - 1];
  assert.equal(
    JSON.parse(retry.options.body).revision,
    9,
    "the next drag must send the freshly reloaded revision, not the stale one"
  );
});
