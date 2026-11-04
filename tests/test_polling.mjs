import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const context = vm.createContext({});
vm.runInContext(readFileSync(new URL("../relay/static/app/polling.js", import.meta.url), "utf8"), context);
const { POLLING, backoffDelay, isPollable, nextPollDecision, afterCheck, nextWakeAt } = context;

const T0 = 1_700_000_000_000;

test("only unknown outcomes are polled", () => {
  for (const status of ["processing", "publishing", "unknown"]) assert.equal(isPollable(status), true);
  for (const status of ["published", "failed", "expired", "ready"]) assert.equal(isPollable(status), false);
});

test("backoff grows and is capped", () => {
  assert.equal(backoffDelay(0), 30_000);
  assert.equal(backoffDelay(1), 60_000);
  assert.equal(backoffDelay(2), 120_000);
  assert.equal(backoffDelay(3), 240_000);
  assert.equal(backoffDelay(4), 300_000);
  assert.equal(backoffDelay(9), 300_000);
});

test("a due destination polls, a waiting one does not", () => {
  const due = nextPollDecision({ status: "processing", attempts: 0, firstCheckedAt: T0, nextCheckAt: T0 }, T0);
  assert.equal(due.poll, true);

  const waiting = nextPollDecision({ status: "processing", attempts: 1, firstCheckedAt: T0, nextCheckAt: T0 + 30_000 }, T0 + 5_000);
  assert.equal(waiting.poll, false);
  assert.equal(waiting.reason, "waiting");
  assert.equal(waiting.delayMs, 25_000);
});

test("a hidden tab never polls", () => {
  const decision = nextPollDecision({ status: "processing", nextCheckAt: T0 }, T0, { hidden: true });
  assert.equal(decision.poll, false);
  assert.equal(decision.reason, "hidden");
});

test("polling stops at the attempt limit", () => {
  const decision = nextPollDecision({ status: "processing", attempts: POLLING.maxAttempts, nextCheckAt: T0 }, T0);
  assert.equal(decision.poll, false);
  assert.equal(decision.reason, "attempt-limit");
});

test("polling stops when the checking window expires", () => {
  const state = { status: "processing", attempts: 2, firstCheckedAt: T0, nextCheckAt: T0 + POLLING.windowMs };
  const decision = nextPollDecision(state, T0 + POLLING.windowMs);
  assert.equal(decision.poll, false);
  assert.equal(decision.reason, "window-expired");
});

test("a check that resolves to a terminal state schedules nothing", () => {
  const next = afterCheck({ status: "published", attempts: 1, firstCheckedAt: T0 }, T0 + 30_000);
  assert.equal(next.nextCheckAt, null);
  assert.equal(next.stopped, "terminal");
});

test("a successful check schedules the next backoff step", () => {
  const next = afterCheck({ status: "processing", attempts: 0, firstCheckedAt: T0 }, T0);
  assert.equal(next.attempts, 1);
  assert.equal(next.nextCheckAt, T0 + 60_000);
  assert.equal(next.stopped, null);
});

test("a failed read backs off to the maximum and keeps the outcome unknown", () => {
  const next = afterCheck({ status: "unknown", attempts: 1, firstCheckedAt: T0 }, T0, { failed: true });
  assert.equal(next.nextCheckAt, T0 + POLLING.maxDelayMs);
  assert.equal(next.stopped, null);
});

test("the attempt limit stops future scheduling", () => {
  const next = afterCheck({ status: "processing", attempts: POLLING.maxAttempts - 1, firstCheckedAt: T0 }, T0);
  assert.equal(next.nextCheckAt, null);
  assert.equal(next.stopped, "attempt-limit");
});

test("the next wake is the earliest pending destination", () => {
  const states = [
    { status: "processing", nextCheckAt: T0 + 60_000 },
    { status: "unknown", nextCheckAt: T0 + 30_000 },
    { status: "published", nextCheckAt: T0 + 5_000 },
  ];
  assert.equal(nextWakeAt(states, T0), T0 + 30_000);
  assert.equal(nextWakeAt([{ status: "processing", nextCheckAt: null }], T0), null);
  assert.equal(nextWakeAt([], T0), null);
});
