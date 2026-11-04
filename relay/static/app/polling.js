"use strict";

/* Delivery polling policy, kept pure so it can be tested with a fake clock.

   Rules:
   - Only destinations whose outcome is still unknown are polled.
   - Delay backs off 30s → 60s → 120s → 240s, capped at 5 minutes.
   - Polling stops after a finite window (20 minutes from the first check) and
     after a maximum number of attempts, so a stuck job cannot poll forever.
   - The window pauses while the tab is hidden and resumes on visibility.
   - A manual check is always allowed and resets the backoff, never the window. */

var POLLING = {
  baseDelayMs: 30_000,
  maxDelayMs: 300_000,
  windowMs: 20 * 60 * 1000,
  maxAttempts: 14,
};

function backoffDelay(attempt) {
  const step = Math.max(0, Math.min(attempt, 4));
  return Math.min(POLLING.baseDelayMs * 2 ** step, POLLING.maxDelayMs);
}

/* Should this destination be polled at all? Terminal and human-blocked states are not. */
function isPollable(status) {
  return status === "processing" || status === "publishing" || status === "unknown";
}

/* Pure decision for one destination.
   state: {status, attempts, firstCheckedAt, nextCheckAt, lastError}
   now: epoch ms
   Returns {poll, reason, delayMs}. */
function nextPollDecision(state, now, { hidden = false } = {}) {
  if (!isPollable(state.status)) return { poll: false, reason: "terminal", delayMs: 0 };
  if (hidden) return { poll: false, reason: "hidden", delayMs: 0 };
  if ((state.attempts || 0) >= POLLING.maxAttempts) {
    return { poll: false, reason: "attempt-limit", delayMs: 0 };
  }
  if (state.firstCheckedAt && now - state.firstCheckedAt >= POLLING.windowMs) {
    return { poll: false, reason: "window-expired", delayMs: 0 };
  }
  if (state.nextCheckAt && now < state.nextCheckAt) {
    return { poll: false, reason: "waiting", delayMs: state.nextCheckAt - now };
  }
  return { poll: true, reason: "due", delayMs: 0 };
}

/* After a completed check, when should the next one be? */
function afterCheck(state, now, { failed = false } = {}) {
  const attempts = (state.attempts || 0) + 1;
  const firstCheckedAt = state.firstCheckedAt || now;
  if (!isPollable(state.status)) return { attempts, firstCheckedAt, nextCheckAt: null, stopped: "terminal" };
  if (attempts >= POLLING.maxAttempts) return { attempts, firstCheckedAt, nextCheckAt: null, stopped: "attempt-limit" };
  if (now - firstCheckedAt >= POLLING.windowMs) return { attempts, firstCheckedAt, nextCheckAt: null, stopped: "window-expired" };
  const delay = failed ? POLLING.maxDelayMs : backoffDelay(attempts);
  return { attempts, firstCheckedAt, nextCheckAt: now + delay, stopped: null };
}

/* The earliest moment any tracked destination wants attention. */
function nextWakeAt(states, now) {
  const times = states
    .filter((state) => isPollable(state.status) && state.nextCheckAt)
    .map((state) => state.nextCheckAt)
    .filter((value) => value > now);
  return times.length ? Math.min(...times) : null;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { POLLING, backoffDelay, isPollable, nextPollDecision, afterCheck, nextWakeAt };
}
