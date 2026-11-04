import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const flow = vm.createContext({});
vm.runInContext(readFileSync(new URL("../relay/static/app/flow-state.js", import.meta.url), "utf8"), flow);

const video = { filename: "Launch.mp4", youtube_compatible: true, instagram_compatible: true };
const photo = { filename: "Still.jpg", youtube_compatible: false, instagram_compatible: true };
const instagram = { id: "ig", provider: "instagram", name: "Studio IG" };
const youtube = { id: "yt", provider: "youtube", name: "Studio YT" };

test("platform sections follow the selected channels, not a master tab", () => {
  assert.deepEqual({ ...flow.composerSections([]) }, { instagram: false, youtube: false });
  assert.deepEqual({ ...flow.composerSections([instagram]) }, { instagram: true, youtube: false });
  assert.deepEqual({ ...flow.composerSections([instagram, youtube]) }, { instagram: true, youtube: true });
});

test("blockers are ordered by what a person would fix first", () => {
  const empty = flow.composerBlockers({ media: null, accounts: [], youtubeTitle: "" });
  assert.deepEqual([...empty].map((b) => b.field), ["media", "accounts"]);

  const noTitle = flow.composerBlockers({ media: video, accounts: [youtube], youtubeTitle: "  " });
  assert.deepEqual([...noTitle].map((b) => b.field), ["youtube-title"]);
  assert.match(noTitle[0].message, /YouTube title/);

  const ready = flow.composerBlockers({ media: video, accounts: [instagram, youtube], youtubeTitle: "Launch" });
  assert.equal(ready.length, 0);

  const incompatible = flow.composerBlockers({ media: photo, accounts: [instagram, youtube], youtubeTitle: "Launch" });
  assert.equal(incompatible.length, 1);
  assert.match(incompatible[0].message, /Studio YT/);
});

test("a photo never becomes publishable for YouTube", () => {
  const blockers = flow.composerBlockers({ media: photo, accounts: [youtube], youtubeTitle: "Launch" });
  assert.ok(blockers.some((b) => b.field === "media"));
});

test("busy and delivered states suppress the publish action", () => {
  assert.equal(flow.composerBlockers({ media: video, accounts: [instagram], publishing: true }).length, 0);
  assert.equal(flow.composerBlockers({ media: video, accounts: [instagram], hasResults: true }).length, 0);
});

test("the action summary states the next concrete step", () => {
  assert.match(flow.composerSummary({ media: null, accounts: [] }).headline, /Choose media/);
  assert.match(flow.composerSummary({ media: video, accounts: [] }).detail, /at least one channel/);
  assert.match(flow.composerSummary({ media: video, accounts: [instagram, youtube] }).headline, /2 channels/);
  // The summary uses the same plain verb as the button ("Sending…"), not the
  // provider's internal word "publishing".
  assert.match(flow.composerSummary({ media: video, accounts: [instagram], publishing: true }).headline, /Sending/);
});

test("delivery distinguishes confirmed, pending, failed, and unconfirmed", () => {
  const confirmed = flow.deliveryOutcome([{ status: "published" }, { url: "https://youtube.com/watch?v=1" }]);
  assert.equal(confirmed.confirmed, 2);
  assert.match(confirmed.headline, /Published to 2 channels/);

  const partial = flow.deliveryOutcome([{ status: "published" }, { status: "processing" }]);
  assert.equal(partial.confirmed, 1);
  assert.equal(partial.pending, 1);
  assert.match(partial.headline, /1 of 2/);

  const failed = flow.deliveryOutcome([{ status: "failed", error: "rejected" }]);
  assert.equal(failed.failed, 1);
  assert.match(failed.headline, /failed/);

  const unknown = flow.deliveryOutcome([{ status: "unknown" }]);
  assert.equal(unknown.unknown, 1);
  assert.match(unknown.headline, /not confirmed/i);

  const transport = flow.deliveryOutcome([{ error: "connection failed", uncertain: true }]);
  assert.equal(transport.unknown, 1, "a transport failure is never counted as a definite failure");
  assert.equal(transport.failed, 0);
});

test("unknown delivery is never reported as success", () => {
  const outcome = flow.deliveryOutcome([{ status: "published" }, { status: "unknown" }]);
  assert.notEqual(outcome.confirmed, outcome.total);
  assert.match(outcome.detail, /unconfirmed/);
});

test("a request Relay refused before dispatch is not a provider failure", () => {
  // Nothing was delivered, but the provider never saw it, so the row must not
  // borrow the provider's verdict.
  const notSent = flow.deliveryOutcome([{ status: "not_sent" }]);
  assert.equal(notSent.notSent, 1);
  assert.equal(notSent.failed, 0);
  assert.equal(notSent.unknown, 0);
  assert.equal(notSent.confirmed, 0);
  assert.match(notSent.headline, /not sent/i);
  assert.match(notSent.detail, /not sent/);
  // A real provider failure keeps its own word.
  const failed = flow.deliveryOutcome([{ status: "failed" }]);
  assert.equal(failed.notSent, 0);
  assert.match(failed.headline, /failed/i);
});

test("draft keys are owned by both user and workspace", () => {
  assert.equal(flow.draftStorageKey({ userId: "u1", workspaceId: "w1" }), "relay:draft:u1:w1");
  assert.notEqual(
    flow.draftStorageKey({ userId: "u1", workspaceId: "w1" }),
    flow.draftStorageKey({ userId: "u2", workspaceId: "w1" }),
  );
  assert.equal(flow.draftStorageKey({ userId: null, workspaceId: "w1" }), null);
  assert.equal(flow.draftStorageKey({ userId: "u1", workspaceId: null }), null);
});

test("legacy draft keys are distinct from owned keys", () => {
  assert.equal(flow.legacyDraftKey("w1"), "relay:draft:w1");
  assert.notEqual(flow.legacyDraftKey("w1"), flow.draftStorageKey({ userId: "u1", workspaceId: "w1" }));
});

test("workspace capacity reports the actual non-resetting cap", () => {
  assert.equal(flow.workspaceCapacity(49), null);
  assert.match(flow.workspaceCapacity(50), /^50\/50 workspaces used\. This cap does not reset/);
});
