"use strict";
const params = new URLSearchParams(window.location.search);
const provider = params.get("provider");
const result = params.get("result");
const names = { instagram: "Instagram", youtube: "YouTube" };
const messages = {
  connected: ["You're connected.", "Your account is saved in Relay. You can close this window and return to your workspace."],
  cancelled: ["Sign-in cancelled.", "Your existing connections haven't changed. Return to Relay whenever you're ready to try again."],
  connection: ["We couldn't finish connecting.", "Check that the callback URL matches your provider settings and that you granted the required permissions. Then try again."],
  expired: ["Let's try that again.", "The sign-in request expired or its browser cookie was lost. Close this window and click Connect again."],
};
if (Object.hasOwn(names, provider) && Object.hasOwn(messages, result)) {
  document.getElementById("oauth-title").textContent = messages[result][0];
  document.getElementById("oauth-description").textContent = messages[result][1];
  document.getElementById("oauth-hint").hidden = true;
  document.getElementById("oauth-return").hidden = false;
  if (window.opener) {
    window.opener.postMessage({ type: "relay:oauth-result", provider, result }, window.location.origin);
  }
}
document.getElementById("oauth-close").addEventListener("click", () => window.close());
