"use strict";

function renderJourney() {
  if (!$("#journey-next-step")) return;
  const media = contentItems.find((item) => item.id === selectedContentId);
  const accounts = accountList.filter((account) => selectedAccountIds.has(account.id));
  const state = postJourneyState({ media, accounts, title: $("#youtube-publish-title").value, busy: publishing, results: [...activeInstagramPosts, ...youtubeUploadResults] });
  $("#publish-view").dataset.state = state.state;
  $("#publish-form").setAttribute("aria-busy", String(publishing));
  $("#journey-next-step").textContent = state.next;
  ["media", "details", "delivery"].forEach((name, i) => {
    const node = $(`#journey-${name}`);
    node.classList.toggle("complete", state.step > i + 1 || state.state === "complete");
    if (state.step === i + 1) node.setAttribute("aria-current", "step");
    else node.removeAttribute("aria-current");
  });
  $("#composer-start").hidden = Boolean(media) || state.step === 3;
  $("#journey-media-copy").textContent = maxUploadBytes ? `JPEG or video · up to ${formatBytes(maxUploadBytes)} per file` : "Choose a saved file or upload something new.";
  const draft = readLocalDraft();
  $("#journey-launch-copy").textContent = draft ? `Your draft${draft.title ? ` “${draft.title}”` : ""} is waiting. Pick up where you left off.` : "Add your media. Make it yours. Send it out.";
  const ready = activeInstagramPosts.some((post) => post.status === "ready");
  const hasResults = activeInstagramPosts.length + youtubeUploadResults.length > 0;
  $("#publish-submit").hidden = hasResults;
  $("#publish-new-post").hidden = !hasResults;
  $("#instagram-publish-ready").hidden = !ready;
  $("#save-draft").hidden = state.state === "complete";
  $("#instagram-publish-ready").classList.toggle("button-primary", ready);
  $("#instagram-publish-ready").classList.toggle("button-secondary", !ready);
  $("#publish-new-post").disabled = publishing || activeInstagramPosts.some((post) => ["processing", "ready", "publishing", "unknown"].includes(post.status));
  $("#publish-new-post").classList.toggle("button-primary", state.state === "complete");
  $("#publish-new-post").classList.toggle("button-ghost", state.state !== "complete");
  $("#publish-new-post").textContent = state.state === "complete" ? "Create next post" : "Edit a copy";
}

function showJourneyRecovery(message, run, container = $("#journey-recovery"), label = "Retry now") {
  container.replaceChildren();
  const copy = document.createElement("span");
  copy.textContent = message;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "button button-secondary button-sm";
  retry.textContent = label;
  retry.addEventListener("click", async () => {
    retry.disabled = true;
    retry.setAttribute("aria-busy", "true");
    try { await run(); } finally { retry.disabled = false; retry.removeAttribute("aria-busy"); }
  });
  container.append(copy, retry);
  container.hidden = false;
}

const channelHome = $("#add-channel").parentElement;
function openChannelSheet() {
  if (publishing) return;
  $("#channel-sheet-slot").append($("#add-channel"));
  $("#channel-sheet").showModal();
}
$("#channel-sheet").addEventListener("close", () => channelHome.append($("#add-channel")));
$("#journey-add-channel").addEventListener("click", openChannelSheet);
$("#journey-add-media").addEventListener("click", () => $("#open-media-picker").click());

function adoptPreviewDraft() {
  try {
    const draft = localStorage.getItem("relay:preview-draft");
    if (!draft || readLocalDraft()) return;
    localStorage.setItem(localDraftKey(), draft);
    localStorage.removeItem("relay:preview-draft");
  } catch { /* A storage restriction must not stop the editor. */ }
}

let sessionWindow = null;
function showSessionRecovery() {
  if (!$("#session-recovery-dialog").open) $("#session-recovery-dialog").showModal();
}
$("#session-resume").addEventListener("click", () => {
  sessionWindow = window.open("/login?resume=1", "relay-session", "popup=yes,width=520,height=720");
  $("#session-popup-error").hidden = Boolean(sessionWindow);
});
$("#session-same-tab").addEventListener("click", () => {
  saveLocalDraft({ announce: false });
  try { sessionStorage.setItem("relay:return-page", activePage); } catch {}
});
window.addEventListener("message", async (event) => {
  if (event.origin !== location.origin || event.source !== sessionWindow || !sessionWindow || event.data?.type !== "relay:session-restored") return;
  sessionWindow.close();
  sessionWindow = null;
  $("#session-recovery-dialog").close();
  notice("Signed in. Continue with the post you were editing.");
});
window.addEventListener("offline", () => {
  $("#connection-status").textContent = "Offline";
  saveLocalDraft({ announce: false });
  showJourneyRecovery("You're offline. Your text stays in this window; file selections are kept until you close it.", async () => {
    try { await api("/session"); $("#journey-recovery").hidden = true; } catch { /* Keep recovery visible. */ }
  });
});
window.addEventListener("online", () => {
  $("#connection-status").textContent = "Online";
  $("#journey-recovery").hidden = true;
});
window.addEventListener("pagehide", () => {
  if (activePage === "publish" && !publishing && $("#publish-view").dataset.state !== "complete") saveLocalDraft({ announce: false });
});
$("#publish-form").addEventListener("input", () => renderJourney());
$(".composer-action-buttons").append($("#instagram-publish-ready"), $("#publish-new-post"));
