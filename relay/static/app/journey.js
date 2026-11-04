"use strict";

/* Inline recovery, the channel sheet, session recovery, and connection status.

   Message discipline: one inline recovery banner at a time. A toast is only for
   a transient confirmation; persistent problems live next to the control that
   caused them. */

function showJourneyRecovery(message, run, container = $("#journey-recovery"), label = "Retry now") {
  if (!container) return;
  container.replaceChildren();
  container.append(el("span", null, message));
  if (run) {
    const retry = el("button", "button button-secondary button-sm", label);
    retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      retry.setAttribute("aria-busy", "true");
      try { await run(); } finally {
        retry.disabled = false;
        retry.removeAttribute("aria-busy");
      }
    });
    container.append(retry);
  }
  container.hidden = false;
}

const channelHome = $("#add-channel").parentElement;
function openChannelSheet() {
  if (publishing) return;
  $("#channel-sheet-slot").append($("#add-channel"));
  $("#channel-sheet-error").hidden = true;
  openDialog($("#channel-sheet"), $("#journey-add-channel"));
}
$("#channel-sheet").addEventListener("close", () => channelHome.append($("#add-channel")));
$("#journey-add-channel").addEventListener("click", () => {
  if (!authenticated) { showUnlock(); return; }
  openChannelSheet();
});

/* The /try preview draft is handled by content.js (renderPreviewConflict):
   it is adopted only when it carries no ownership claim and the current user
   has no draft of their own, and otherwise the person chooses between the two
   captions. It is never moved between users. */

/* ── Session recovery ──────────────────────────────────────── */

let sessionWindow = null;
function showSessionRecovery() {
  const dialog = $("#session-recovery-dialog");
  if (!dialog.open) openDialog(dialog);
}
$("#session-resume").addEventListener("click", () => {
  sessionWindow = window.open("/login?resume=1", "relay-session", "popup=yes,width=520,height=720");
  $("#session-popup-error").hidden = Boolean(sessionWindow);
});
$("#session-same-tab").addEventListener("click", () => {
  saveLocalDraft({ announce: false });
  try { sessionStorage.setItem("relay:return-page", activePage); } catch { /* ignore */ }
});
window.addEventListener("message", async (event) => {
  if (event.origin !== location.origin || event.source !== sessionWindow || !sessionWindow || event.data?.type !== "relay:session-restored") return;
  sessionWindow.close();
  sessionWindow = null;
  $("#session-recovery-dialog").close();
  notice("Signed in. Your post is still here.");
});

/* ── Connection status ─────────────────────────────────────── */

function renderConnectionStatus() {
  const status = $("#connection-status");
  if (!status) return;
  status.hidden = navigator.onLine;
  status.textContent = "Offline";
}

window.addEventListener("offline", () => {
  renderConnectionStatus();
  saveLocalDraft({ announce: false });
  showJourneyRecovery("You're offline. Your text stays in this window and your draft is saved in this browser.", async () => {
    try { await api("/session"); $("#journey-recovery").hidden = true; renderConnectionStatus(); } catch { /* keep the banner */ }
  });
});
window.addEventListener("online", () => {
  renderConnectionStatus();
  $("#journey-recovery").hidden = true;
});

window.addEventListener("pagehide", () => {
  if (activePage === "publish" && !publishing) {
    saveLocalDraft({ announce: false });
  }
});
