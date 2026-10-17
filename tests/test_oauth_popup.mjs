import assert from "node:assert/strict";
import test from "node:test";
import { OAuthPopup } from "../relay/static/oauth-popup.mjs";

function browser({ blocked = false } = {}) {
  const events = [];
  const popup = {
    closed: false,
    location: { replace: (url) => events.push(["navigate", url]) },
    focus: () => events.push(["focus"]),
    close() { this.closed = true; events.push(["close"]); },
  };
  const host = {
    location: { origin: "http://localhost:8000" }, screenX: 0, screenY: 0,
    outerWidth: 1280, outerHeight: 900,
    open(...args) { events.push(["open", ...args]); return blocked ? null : popup; },
    addEventListener(type, handler) { this.listener = handler; },
    removeEventListener() { this.listener = null; },
    setInterval(handler) { this.tick = handler; return 1; },
    clearInterval() { this.tick = null; },
  };
  return { host, popup, events };
}

test("opens a small window immediately and leaves the dashboard in place", async () => {
  const {host, events} = browser();
  const flow = new OAuthPopup(host, () => {}, () => {});
  flow.open("youtube");
  assert.equal(events[0][0], "open");
  assert.equal(events[0][1], "/oauth/wait");
  assert.match(events[0][3], /popup=yes,width=520,height=720/);
  await Promise.resolve(); // The authorization API can complete later without losing user activation.
  flow.navigate("https://accounts.google.com/o/oauth2/v2/auth?prompt=consent+select_account");
  assert.equal(events.at(-2)[0], "navigate");
  assert.equal(host.location.origin, "http://localhost:8000");
  flow.close();
});

test("blocked popups produce actionable feedback without navigating the dashboard", () => {
  const {host} = browser({blocked: true});
  const flow = new OAuthPopup(host, () => {}, () => {});
  assert.throws(() => flow.open("youtube"), /Allow popups/);
  assert.equal(flow.active, false);
});

test("accepts completion only from the expected window, origin, and provider", () => {
  const {host, popup} = browser();
  const results = [];
  const flow = new OAuthPopup(host, (data) => results.push(data), () => {});
  flow.open("youtube");
  const data = {type: "relay:oauth-result", provider: "youtube", result: "connected"};
  host.listener({source: popup, origin: "https://attacker.example", data});
  host.listener({source: {}, origin: host.location.origin, data});
  host.listener({source: popup, origin: host.location.origin, data: {...data, provider: "instagram"}});
  assert.equal(results.length, 0);
  host.listener({source: popup, origin: host.location.origin, data});
  assert.deepEqual(results, [data]);
  assert.equal(popup.closed, true);
  assert.equal(host.listener, null);
  assert.equal(host.tick, null);
});

test("closing sign-in resets the flow and notifies the dashboard once", () => {
  const {host, popup} = browser();
  const closed = [];
  const flow = new OAuthPopup(host, () => {}, (provider) => closed.push(provider));
  flow.open("instagram");
  const tick = host.tick;
  popup.closed = true;
  tick();
  assert.deepEqual(closed, ["instagram"]);
  assert.equal(flow.active, false);
  assert.equal(host.tick, null);
});

test("reuse focuses an existing popup and avoids duplicate sign-in requests", () => {
  const {host, events} = browser();
  const flow = new OAuthPopup(host, () => {}, () => {});
  assert.equal(flow.open("youtube"), true);
  assert.equal(flow.open("youtube"), false);
  assert.equal(events.filter(([event]) => event === "open").length, 1);
  flow.close();
});

test("only the provider's HTTPS authorization host can receive popup navigation", () => {
  const {host} = browser();
  const flow = new OAuthPopup(host, () => {}, () => {});
  flow.open("youtube");
  for (const url of ["https://attacker.example", "https://accounts.google.com.attacker.example", "http://accounts.google.com", "https://www.instagram.com/oauth/authorize"]) {
    assert.throws(() => flow.navigate(url), /unexpected sign-in address/);
  }
  flow.close();
});
