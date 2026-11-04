import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const context = vm.createContext({});
vm.runInContext(readFileSync(new URL("../relay/static/app/api-client.js", import.meta.url), "utf8"), context);
const { parseApiResponse, safeDetail, statusMessage } = context;

test("a JSON error keeps its HTTP status and a readable detail", () => {
  const result = parseApiResponse(409, JSON.stringify({ detail: "Reconnect this account." }));
  assert.equal(result.ok, false);
  assert.equal(result.status, 409);
  assert.equal(result.message, "Reconnect this account.");
});

test("a non-JSON error body never leaks the raw text", () => {
  const result = parseApiResponse(502, "<html><body>Bad Gateway</body></html>");
  assert.equal(result.ok, false);
  assert.equal(result.status, 502);
  assert.doesNotMatch(result.message, /<html|Bad Gateway/);
  assert.match(result.message, /didn't respond as expected/);
});

test("an empty error body falls back to a status message, not a parser error", () => {
  const result = parseApiResponse(500, "");
  assert.equal(result.ok, false);
  assert.match(result.message, /unexpected problem/);
  assert.doesNotMatch(result.message, /JSON|parse|undefined/);
});

test("a storage 503 carries its outcome so an unknown write is never retried", () => {
  const notApplied = parseApiResponse(503, JSON.stringify({
    detail: "Could not record this post safely. Nothing was sent for this request. Try again.",
    outcome: "not_applied",
  }));
  assert.equal(notApplied.outcome, "not_applied");
  const unknown = parseApiResponse(503, JSON.stringify({
    detail: "Instagram accepted the publish, but Relay could not record it. Check the post on Instagram before doing anything else.",
    outcome: "unknown",
  }));
  assert.equal(unknown.outcome, "unknown");
  // The outcome is never inferred from the status code alone.
  assert.equal(parseApiResponse(503, JSON.stringify({ detail: "Try again." })).outcome, null);
  assert.equal(parseApiResponse(500, JSON.stringify({ outcome: "not_applied" })).outcome, "not_applied");
  // An unrecognised outcome is not silently treated as safe.
  assert.equal(parseApiResponse(503, JSON.stringify({ outcome: "maybe" })).outcome, null);
});

test("known statuses have specific copy", () => {
  assert.match(statusMessage(401), /session ended/);
  assert.match(statusMessage(413), /larger/);
  assert.match(statusMessage(429), /Too many attempts/);
  assert.match(statusMessage(418), /couldn't finish that request/);
});

test("internal diagnostics are replaced rather than shown", () => {
  const env = parseApiResponse(500, JSON.stringify({ detail: "Set INSTAGRAM_MEDIA_BUCKET on the Relay service" }));
  assert.doesNotMatch(env.message, /INSTAGRAM_MEDIA_BUCKET/);

  const trace = parseApiResponse(500, JSON.stringify({ detail: "Traceback (most recent call last): File app.py" }));
  assert.doesNotMatch(trace.message, /Traceback/);

  const stack = parseApiResponse(500, JSON.stringify({ detail: "TypeError at handler (app.js:42)" }));
  assert.doesNotMatch(stack.message, /app\.js/);
});

test("provider bodies are never forwarded, even when short", () => {
  const provider = parseApiResponse(400, JSON.stringify({
    error: { message: "Error validating access token: Session has expired", type: "OAuthException", code: 190 },
  }));
  assert.equal(provider.ok, false);
  assert.doesNotMatch(provider.message, /OAuthException|access token/);
  assert.match(provider.message, /couldn't accept that request/);
});

test("a successful JSON body is returned as data", () => {
  const result = parseApiResponse(200, JSON.stringify({ accounts: [] }));
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.data)), { accounts: [] });
  assert.equal(result.malformed, false);
});

test("a 2xx with an unreadable body is flagged, not silently accepted", () => {
  const result = parseApiResponse(200, "not json");
  assert.equal(result.ok, true);
  assert.equal(result.malformed, true);
});

test("an empty 2xx body is not treated as malformed", () => {
  assert.equal(parseApiResponse(204, "").malformed, false);
});

test("safeDetail rejects non-strings, overlong text, and blank strings", () => {
  assert.equal(safeDetail(null), null);
  assert.equal(safeDetail({ detail: "x" }), null);
  assert.equal(safeDetail("   "), null);
  assert.equal(safeDetail("x".repeat(500)), null);
  assert.equal(safeDetail("  A short message.  "), "A short message.");
});
