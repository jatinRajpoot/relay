import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

/* Round-8 DOM-level probes.

   These exercise the real publishing.js render path with a minimal fake DOM, so
   the ordering defects the review found are reproduced and fixed behaviourally
   rather than asserted from source text.

   The fake DOM is deliberately tiny: every element is a generic node, and
   `document.querySelector` auto-creates and memoises a node per selector. That
   is enough for the delivery renderer, which only sets text, classes, and
   children. No helper from composer.js/core.js is loaded: `el`, `icon`, and
   `makeProfileAvatar` are stubs so this file tests publishing.js alone. */

/* ── Fake DOM ──────────────────────────────────────────────── */

function makeClassList(node) {
  const set = new Set();
  return {
    add: (...names) => names.forEach((name) => set.add(name)),
    remove: (...names) => names.forEach((name) => set.delete(name)),
    contains: (name) => set.has(name),
    toggle: (name, force) => {
      const on = force === undefined ? !set.has(name) : Boolean(force);
      if (on) set.add(name);
      else set.delete(name);
      return on;
    },
    get value() { return [...set].join(" "); },
  };
}

function makeNode(tag = "div") {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    attributes: {},
    dataset: {},
    hidden: false,
    disabled: false,
    value: "",
    checked: false,
    open: false,
    title: "",
    type: "",
    id: "",
    className: "",
    listeners: {},
    textContent: "",
  };
  node.classList = makeClassList(node);
  node.append = (...items) => {
    for (const item of items) {
      if (item == null) continue;
      node.children.push(typeof item === "string" ? { textContent: item, children: [] } : item);
    }
  };
  node.appendChild = (item) => { node.append(item); return item; };
  node.replaceChildren = (...items) => { node.children = []; node.append(...items); };
  node.setAttribute = (name, value) => {
    node.attributes[name] = String(value);
    if (name === "id") node.id = String(value);
    if (name === "class") node.className = String(value);
    if (name === "role") node.role = String(value);
  };
  node.getAttribute = (name) => (name in node.attributes ? node.attributes[name] : null);
  node.removeAttribute = (name) => { delete node.attributes[name]; };
  node.hasAttribute = (name) => name in node.attributes;
  node.addEventListener = (name, handler) => { (node.listeners[name] ||= []).push(handler); };
  node.remove = () => { node.removed = true; };
  // <dialog> surface: openDialog/showModal/close are what the copy flow uses.
  node.showModal = () => { node.open = true; };
  node.close = () => { node.open = false; };
  node.focus = () => {};
  node.scrollIntoView = () => {};
  node.querySelector = (selector) => find(node, selector) || makeNode();
  node.querySelectorAll = (selector) => findAll(node, selector);
  node.contains = (other) => {
    if (other === node) return true;
    return node.children.some((child) => child && typeof child === "object" && child.contains && child.contains(other));
  };
  Object.defineProperty(node, "childElementCount", { get: () => node.children.length });
  Object.defineProperty(node, "firstElementChild", { get: () => node.children[0] || null });
  Object.defineProperty(node, "lastElementChild", { get: () => node.children[node.children.length - 1] || null });
  return node;
}

function matches(node, selector) {
  if (!node || typeof node !== "object") return false;
  if (selector.startsWith("#")) return node.id === selector.slice(1);
  if (selector.startsWith(".")) return String(node.className || "").split(/\s+/).includes(selector.slice(1));
  return node.tagName === selector.toUpperCase();
}

function find(node, selector) {
  for (const child of node.children) {
    if (!child || typeof child !== "object") continue;
    if (matches(child, selector)) return child;
    const deeper = find(child, selector);
    if (deeper) return deeper;
  }
  return null;
}

function findAll(node, selector, out = []) {
  for (const child of node.children) {
    if (!child || typeof child !== "object") continue;
    if (matches(child, selector)) out.push(child);
    findAll(child, selector, out);
  }
  return out;
}

function textOf(node) {
  if (!node) return "";
  if (typeof node === "string") return node;
  let text = node.textContent || "";
  for (const child of node.children || []) text += textOf(child);
  return text;
}

function buttonsIn(node) {
  return findAll(node, "button");
}

function document_() {
  const registry = new Map();
  const body = makeNode("body");
  const root = makeNode("html");
  const doc = {
    body,
    documentElement: root,
    activeElement: body,
    hidden: false,
    title: "",
    createElement: (tag) => makeNode(tag),
    createElementNS: (_ns, tag) => makeNode(tag),
    addEventListener: () => {},
    querySelectorAll: () => [],
    querySelector(selector) {
      if (!registry.has(selector)) {
        const node = makeNode("div");
        if (selector.startsWith("#")) node.id = selector.slice(1);
        registry.set(selector, node);
        body.append(node);
      }
      return registry.get(selector);
    },
    getElementById(id) {
      if (body.id === id) return body;
      return find(body, `#${id}`);
    },
    contains: (node) => body.contains(node),
  };
  return doc;
}

/* ── Harness ───────────────────────────────────────────────── */

const APP = new URL("../relay/static/app/", import.meta.url);

function source(file) {
  return readFileSync(new URL(file, APP), "utf8");
}

function buildHarness({ api } = {}) {
  const doc = document_();
  const notices = [];
  const calls = [];
  const sandbox = {
    window: {
      location: { origin: "https://relay.example" },
      setTimeout: (fn) => { fn(); return 0; },
      clearTimeout: () => {},
      scrollTo: () => {},
    },
    URL,
    URLSearchParams,
    FormData,
    console,
    document: doc,
    // Shared page state normally declared by journey.js.
    publishing: false,
    accountList: [
      { id: "acct-1", provider: "instagram", name: "Field Notes Archive", expires_at: 2_000_000_000, credentials_readable: true },
      { id: "acct-2", provider: "youtube", name: "Field Notes Films", expires_at: 2_000_000_000, credentials_readable: true },
    ],
    contentItems: [],
    selectedContentId: null,
    selectedAccountIds: new Set(),
    activeInstagramPosts: [],
    youtubeUploadResults: [],
    activePage: "publish",
    activeUserId: "user-1",
    activeWorkspaceId: "ws-1",
    authenticated: true,
    instagramMediaConfigured: true,
    contentStorageConfigured: true,
    instagramStatusCheckRunning: false,
    instagramStatusTimer: null,
    historyLoaded: false,
    names: { instagram: "Instagram", youtube: "YouTube" },
    // Helpers from other modules, stubbed so only publishing.js is under test.
    $: (selector) => doc.querySelector(selector),
    el: (tag, className, text) => {
      const node = makeNode(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    },
    setText: (selector, value) => { doc.querySelector(selector).textContent = value; },
    icon: () => makeNode("svg"),
    makeProfileAvatar: () => makeNode("span"),
    notice: (message, error = false, action = null) => notices.push({ message, error, action }),
    saveLocalDraft: () => true,
    readLocalDraft: () => sandbox.__draft,
    restoreLocalDraft: () => true,
    renderComposer: () => {},
    renderComposerActions: () => {},
    renderHomeDraft: () => {},
    renderPostHistory: () => {},
    setPage: () => {},
    connect: () => {},
    openDialog: (dialog) => { dialog.open = true; },
    formatBytes: (bytes) => `${bytes} B`,
    api: api || (async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      return { attempts: [], next_cursor: null };
    }),
  };
  sandbox.globalThis = sandbox;
  sandbox.__draft = null;
  sandbox.__calls = calls;
  sandbox.__notices = notices;
  const context = vm.createContext(sandbox);
  const script = [
    source("flow-state.js"),
    source("recovery.js"),
    source("polling.js"),
    source("publishing.js"),
    /* eslint-disable no-undef */
    `globalThis.__publishing = {
      loadRecoveredAttempts,
      renderDeliverySection,
      renderRecoveredSection,
      editCopyFor,
      resolveCopyChoice,
      checkRecovered,
      currentResults,
      receiptLookupState,
      getState: () => ({ activeSubmission, recoveredAttempts, recoveredLoaded, recoveredError, recoveredLoading, receiptsSyncing }),
      setState: (patch) => {
        if ("activeSubmission" in patch) activeSubmission = patch.activeSubmission;
        if ("recoveredAttempts" in patch) recoveredAttempts = patch.recoveredAttempts;
        if ("recoveredLoaded" in patch) recoveredLoaded = patch.recoveredLoaded;
        if ("recoveredError" in patch) recoveredError = patch.recoveredError;
        if ("recoveredLoading" in patch) recoveredLoading = patch.recoveredLoading;
        if ("receiptsSyncing" in patch) receiptsSyncing = patch.receiptsSyncing;
        if ("instagramPosts" in patch) activeInstagramPosts = patch.instagramPosts;
        if ("youtubeResults" in patch) youtubeUploadResults = patch.youtubeResults;
      },
      pendingCopyChoice: () => pendingCopyChoice,
      copySourceForResult,
      composerHasUnsentWork,
    };`,
  ].join("\n");
  vm.runInContext(script, context);
  return { sandbox, doc, publishing: sandbox.__publishing, notices, calls };
}

const IG = { id: "acct-1", provider: "instagram", name: "Field Notes Archive" };

function failedResult(overrides = {}) {
  return {
    account: IG,
    id: null,
    attemptId: null,
    status: "failed",
    error: "Provider authorization expired or was revoked. Reconnect account.",
    updatedAt: 1_700_000_000_000,
    attempts: 0,
    firstCheckedAt: null,
    nextCheckAt: null,
    ...overrides,
  };
}

function reconnectReceipt(overrides = {}) {
  return {
    id: "attempt-1",
    provider: "instagram",
    account_id: "acct-1",
    status: "failed",
    request_key: REQUEST_KEY,
    error: "Provider authorization expired or was revoked. Reconnect account.",
    retry_action: { kind: "reconnect", endpoint: null, key: "none", retry_at: null },
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...overrides,
  };
}

function submissionFor(accountId, provider = "instagram", key = null) {
  return {
    id: "sub-1",
    userId: "user-1",
    workspaceId: "ws-1",
    createdAt: 1_700_000_000_000,
    destinations: {
      [accountId]: {
        provider,
        payloads: {},
        keys: key ? { [provider === "youtube" ? "upload" : "create"]: key } : {},
        state: "failed",
      },
    },
  };
}

const REQUEST_KEY = "relay.abc1234.sub1.acct1.create.payload1";

function rowButtons(doc) {
  return buttonsIn(doc.querySelector("#publish-results"));
}

/* ── 2. Immediate post-failure action truth ────────────────── */

test("before the receipt read resolves, a failed row does not offer a copy", () => {
  const { doc, publishing } = buildHarness();
  publishing.setState({
    instagramPosts: [failedResult()],
    activeSubmission: submissionFor("acct-1", "instagram", REQUEST_KEY),
    recoveredAttempts: [],
    recoveredLoaded: false,
    recoveredLoading: false,
    receiptsSyncing: true,
  });
  publishing.renderDeliverySection();
  const buttons = rowButtons(doc);
  assert.equal(buttons.length, 1, "one control while the record is being read");
  assert.equal(buttons[0].textContent, "Checking…");
  assert.equal(buttons[0].disabled, true);
  // The exact round-7 defect: this used to be "Edit a copy" here.
  assert.notEqual(buttons[0].textContent, "Edit a copy");
});

test("the delivery row re-renders from the receipt once the read lands", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { doc, publishing, calls } = buildHarness({
    api: async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      await gate;
      return { attempts: [reconnectReceipt()], next_cursor: null };
    },
  });
  publishing.setState({
    instagramPosts: [failedResult()],
    activeSubmission: submissionFor("acct-1", "instagram", REQUEST_KEY),
    recoveredAttempts: [],
    recoveredLoaded: false,
    recoveredLoading: false,
    receiptsSyncing: true,
  });
  publishing.renderDeliverySection();
  assert.equal(rowButtons(doc)[0].textContent, "Checking…");

  const pending = publishing.loadRecoveredAttempts();
  // The read is still in flight: the row must not have changed yet.
  assert.equal(rowButtons(doc)[0].textContent, "Checking…");
  publishing.setState({ receiptsSyncing: false });
  release();
  await pending;

  // No manual re-render and no page reload: the row now derives its action from
  // the same receipt the "Earlier deliveries" list uses.
  const buttons = rowButtons(doc);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Reconnect channel");
  assert.notEqual(buttons[0].textContent, "Edit a copy");
  // Reading receipts never dispatched anything.
  assert.ok(calls.every((call) => call.method === "GET"), "a receipt read is read-only");
});

test("a failed receipt read leaves the row uncertain, never a new attempt", async () => {
  const { doc, publishing } = buildHarness({
    api: async () => { throw new Error("Relay couldn't finish that request."); },
  });
  publishing.setState({
    instagramPosts: [failedResult()],
    activeSubmission: submissionFor("acct-1", "instagram", REQUEST_KEY),
    recoveredAttempts: [],
    recoveredLoaded: false,
    recoveredLoading: false,
    receiptsSyncing: false,
  });
  await publishing.loadRecoveredAttempts();
  const buttons = rowButtons(doc);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Check status");
  assert.notEqual(buttons[0].textContent, "Edit a copy");
  const state = publishing.getState();
  assert.equal(state.recoveredError !== null, true);
});

/* ── 3. Cooldown: read-only refresh, no dispatch ───────────── */

function cooldownReceipt() {
  return reconnectReceipt({
    retry_action: { kind: "wait", endpoint: null, key: "none", retry_at: 1_700_000_900 },
    error: "The provider rate-limited this request.",
  });
}

test("a cooldown row offers Check availability, and checking dispatches nothing", async () => {
  const calls = [];
  const { doc, publishing } = buildHarness({
    api: async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      return { attempts: [cooldownReceipt()], next_cursor: null };
    },
  });
  publishing.setState({
    instagramPosts: [failedResult({ attemptId: "attempt-1", receipt: cooldownReceipt() })],
    activeSubmission: submissionFor("acct-1", "instagram", REQUEST_KEY),
    recoveredAttempts: [cooldownReceipt()],
    recoveredLoaded: true,
    recoveredLoading: false,
    receiptsSyncing: false,
  });
  publishing.renderDeliverySection();
  const buttons = rowButtons(doc);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Check availability");
  assert.notEqual(buttons[0].textContent, "Edit a copy", "a copy would bypass the wait");

  // Click it: only read-only receipt reads may occur.
  await buttons[0].listeners.click[0]();
  assert.ok(calls.length > 0, "the check did read the receipt");
  assert.ok(calls.every((call) => call.method === "GET"), `no dispatch while checking: ${JSON.stringify(calls)}`);
  assert.ok(calls.every((call) => !call.path.includes("/instagram/posts")), "no create or publish call");
});

test("after the wait elapses the same row becomes the server's own recipe", () => {
  const { doc, publishing } = buildHarness();
  const afterWait = reconnectReceipt({
    request_key: "relay.abc1234.sub1.acct1.create.payload1",
    // The provider's rate limit has elapsed, so the server now names a resend.
    error: "The provider rate-limited this request.",
    retry_action: { kind: "resend", endpoint: "/instagram/posts", key: "request", retry_at: null },
  });
  publishing.setState({
    instagramPosts: [failedResult({ attemptId: "attempt-1", receipt: afterWait })],
    activeSubmission: submissionFor("acct-1", "instagram", REQUEST_KEY),
    recoveredAttempts: [afterWait],
    recoveredLoaded: true,
    recoveredLoading: false,
    receiptsSyncing: false,
  });
  publishing.renderDeliverySection();
  const buttons = rowButtons(doc);
  assert.equal(buttons[0].textContent, "Try again");
});

/* ── 4. Edit a copy replaces atomically, with an explicit choice ── */

const library = [
  { id: "c1", filename: "harbour.mp4", media_url: "https://storage.example/harbour.mp4", content_type: "video/mp4" },
  { id: "c2", filename: "still.jpg", media_url: "https://storage.example/still.jpg", content_type: "image/jpeg" },
];

/* The UI always hands editCopyFor the row it rendered, which carries the exact
   receipt. Passing a hand-built stub would test a call shape no caller uses. */
function rowFor(publishing, accountId) {
  const row = publishing.currentResults().find((entry) => entry.account.id === accountId);
  assert.ok(row, `rendered row for ${accountId}`);
  return row;
}

function copyHarness() {
  const harness = buildHarness();
  harness.sandbox.contentItems = library;
  harness.sandbox.accountList = [
    { id: "acct-1", provider: "instagram", name: "Field Notes Archive", expires_at: 2_000_000_000, credentials_readable: true },
    { id: "acct-2", provider: "youtube", name: "Field Notes Films", expires_at: 2_000_000_000, credentials_readable: true },
  ];
  return harness;
}

test("a copy never overwrites unsent work without a choice", () => {
  const { sandbox, doc, publishing, notices } = copyHarness();
  doc.querySelector("#publish-caption").value = "My unrelated draft";
  sandbox.selectedContentId = "c2";
  publishing.setState({
    instagramPosts: [
      failedResult({ attemptId: "attempt-1", receipt: reconnectReceipt({ caption: "The tide came in.", media_url: "https://storage.example/still.jpg" }) }),
      { account: { id: "acct-2", provider: "youtube" }, status: "published", url: "https://youtu.be/1" },
    ],
    recoveredAttempts: [],
    recoveredLoaded: true,
  });
  publishing.editCopyFor(rowFor(publishing, "acct-1"));

  // Nothing changed yet: the composer is exactly as the person left it.
  assert.equal(doc.querySelector("#publish-caption").value, "My unrelated draft");
  assert.equal(sandbox.selectedContentId, "c2");
  assert.equal(doc.getElementById("copy-choice-dialog").open, true);
  assert.ok(publishing.pendingCopyChoice(), "the pending choice is held");
  assert.equal(notices.length, 0, "no toast claims a copy happened");
});

test("Keep leaves the composer untouched", () => {
  const { sandbox, doc, publishing, notices } = copyHarness();
  doc.querySelector("#publish-caption").value = "My unrelated draft";
  sandbox.selectedContentId = "c2";
  publishing.setState({
    instagramPosts: [
      failedResult({ attemptId: "attempt-1", receipt: reconnectReceipt({ caption: "The tide came in.", media_url: "https://storage.example/still.jpg" }) }),
    ],
    recoveredAttempts: [],
    recoveredLoaded: true,
  });
  publishing.editCopyFor(rowFor(publishing, "acct-1"));
  publishing.resolveCopyChoice(false);
  assert.equal(doc.querySelector("#publish-caption").value, "My unrelated draft");
  assert.equal(sandbox.selectedContentId, "c2");
  assert.equal(publishing.pendingCopyChoice(), null);
  assert.match(notices.at(-1).message, /Kept your current post/);
});

test("Replace applies the receipt's content exactly, never a blend", () => {
  const { sandbox, doc, publishing, notices } = copyHarness();
  doc.querySelector("#publish-caption").value = "My unrelated draft";
  doc.querySelector("#youtube-publish-title").value = "Unrelated title";
  doc.querySelector("#youtube-publish-description").value = "Unrelated description";
  sandbox.selectedContentId = "c2";
  publishing.setState({
    instagramPosts: [
      failedResult({ attemptId: "attempt-1", receipt: reconnectReceipt({ caption: "The tide came in.", media_url: "https://storage.example/still.jpg" }) }),
      { account: { id: "acct-2", provider: "youtube" }, status: "published", url: "https://youtu.be/1" },
    ],
    recoveredAttempts: [],
    recoveredLoaded: true,
  });
  publishing.editCopyFor(rowFor(publishing, "acct-1"));
  publishing.resolveCopyChoice(true);

  // The receipt's caption replaces the draft; unrelated YouTube text is cleared
  // rather than carried into an Instagram copy.
  assert.equal(doc.querySelector("#publish-caption").value, "The tide came in.");
  assert.equal(doc.querySelector("#youtube-publish-title").value, "");
  assert.equal(doc.querySelector("#youtube-publish-description").value, "");
  assert.equal(sandbox.selectedContentId, "c2", "the receipt's own media, not the draft's");
  // Only unresolved destinations are selected: the published YouTube row is not.
  assert.deepEqual([...sandbox.selectedAccountIds], ["acct-1"]);
  assert.match(notices.at(-1).message, /Copied for a new attempt/);
});

test("a YouTube copy restores title and description into their own fields", () => {
  const { sandbox, doc, publishing } = copyHarness();
  const youtube = { id: "acct-2", provider: "youtube", name: "Field Notes Films" };
  publishing.setState({
    youtubeResults: [
      {
        account: youtube,
        status: "failed",
        attemptId: "attempt-yt",
        receipt: reconnectReceipt({
          id: "attempt-yt",
          provider: "youtube",
          account_id: "acct-2",
          title: "Harbour at dawn",
          caption: "A quiet morning on the water.",
          media_name: "harbour.mp4",
          retry_action: { kind: "reconnect", endpoint: null, key: "none", retry_at: null },
        }),
      },
    ],
    recoveredAttempts: [],
    recoveredLoaded: true,
  });
  publishing.editCopyFor(rowFor(publishing, "acct-2"));
  assert.equal(doc.querySelector("#youtube-publish-title").value, "Harbour at dawn");
  assert.equal(doc.querySelector("#youtube-publish-description").value, "A quiet morning on the water.");
  // The YouTube description is never written into the Instagram caption.
  assert.equal(doc.querySelector("#publish-caption").value, "");
  assert.equal(sandbox.selectedContentId, "c1", "the video is restored from the library");
  assert.deepEqual([...sandbox.selectedAccountIds], ["acct-2"]);
});

test("an unresolvable copy says what it copied and what to choose", () => {
  const { sandbox, doc, publishing, notices } = copyHarness();
  sandbox.contentItems = [];
  publishing.setState({
    youtubeResults: [
      {
        account: { id: "acct-2", provider: "youtube", name: "Field Notes Films" },
        status: "failed",
        attemptId: "attempt-yt",
        receipt: reconnectReceipt({
          id: "attempt-yt",
          provider: "youtube",
          account_id: "acct-2",
          title: "Harbour at dawn",
          media_name: "harbour.mp4",
          retry_action: { kind: "reconnect", endpoint: null, key: "none", retry_at: null },
        }),
      },
    ],
    recoveredAttempts: [],
    recoveredLoaded: true,
  });
  publishing.editCopyFor(rowFor(publishing, "acct-2"));
  assert.equal(doc.querySelector("#youtube-publish-title").value, "Harbour at dawn");
  assert.equal(sandbox.selectedContentId, null, "no wrong media is chosen");
  const message = notices.at(-1).message;
  assert.match(message, /Title copied/);
  assert.match(message, /harbour\.mp4/);
  assert.doesNotMatch(message, /Copied for a new attempt/);
});

/* ── 6. Uncorrelated banner in the rendered DOM ────────────── */

test("the rendered uncorrelated banner is one plain sentence", () => {
  const { doc, publishing } = buildHarness();
  publishing.setState({
    activeSubmission: {
      id: "sub-1", userId: "u", workspaceId: "w", createdAt: 1_700_000_000_000,
      destinations: { "acct-1": { provider: "instagram", payloads: {}, keys: {}, state: "sent" } },
    },
    recoveredAttempts: [reconnectReceipt({ id: "other", created_at: 1_700_000_100, status: "failed" })],
    recoveredLoaded: true,
    recoveredLoading: false,
  });
  publishing.renderRecoveredSection();
  const note = doc.querySelector("#recovered-unassociated");
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /listed separately/);
  assert.match(note.textContent, /nothing was resent/);
  assert.doesNotMatch(note.textContent, /exact reference|server last recorded|ledger/i);
  assert.ok(note.textContent.length < 220);
});
