import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const flow = vm.createContext({});
vm.runInContext(readFileSync(new URL("../relay/static/app/flow-state.js", import.meta.url), "utf8"), flow);
const video = { filename: "Launch.mp4", youtube_compatible: true, instagram_compatible: true };
const youtube = { id: "yt", provider: "youtube" };
const base = { media: video, accounts: [youtube], title: "Launch", busy: false, results: [] };

test("the core journey preserves partial input and exposes the missing requirement", () => {
  assert.equal(flow.postJourneyState({ ...base, media: null }).state, "empty");
  assert.equal(flow.postJourneyState({ ...base, accounts: [] }).step, 2);
  assert.match(flow.postJourneyState({ ...base, title: " " }).next, /YouTube title/);
  assert.equal(flow.postJourneyState(base).state, "ready");
  assert.equal(base.title, "Launch");
});
test("incompatible files never become ready", () => {
  const state = flow.postJourneyState({ ...base, media: { ...video, youtube_compatible: false } });
  assert.equal(state.state, "partial");
  assert.match(state.next, /compatible file/);
});
test("delivery distinguishes accepted, ready, confirmed, and partial failure", () => {
  assert.equal(flow.postJourneyState({ ...base, busy: true }).state, "loading");
  assert.equal(flow.postJourneyState({ ...base, results: [{ status: "processing" }] }).state, "processing");
  assert.match(flow.postJourneyState({ ...base, results: [{ status: "ready" }] }).next, /1 ready for Instagram confirmation/);
  assert.equal(flow.postJourneyState({ ...base, results: [{ url: "https://youtube.com/watch?v=1" }] }).state, "complete");
  const state = flow.postJourneyState({ ...base, results: [{ status: "published" }, { error: "offline" }] });
  assert.equal(state.state, "recovery");
  assert.match(state.next, /1\/2 destinations confirmed/);
});
test("unknown publication cannot be presented as confirmed", () => {
  assert.equal(flow.postJourneyState({ ...base, results: [{ status: "unknown" }] }).state, "recovery");
});
test("workspace capacity reports the actual non-resetting cap", () => {
  assert.equal(flow.workspaceCapacity(49), null);
  assert.match(flow.workspaceCapacity(50), /^50\/50 workspaces used\. This cap does not reset/);
});
