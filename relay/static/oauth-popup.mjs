// Own the sign-in window separately from the dashboard's data and presentation.
// open() must run directly in a click/submit handler, before any network awaits.
export class OAuthPopup {
  constructor(host, onResult, onClosed) {
    this.host = host;
    this.onResult = onResult;
    this.onClosed = onClosed;
    this.popup = null;
    this.provider = null;
    this.timer = null;
    this.listener = (event) => this.receive(event);
  }

  get active() { return Boolean(this.popup); }

  open(provider) {
    if (this.popup && !this.popup.closed) {
      this.popup.focus();
      return false;
    }
    const width = 520;
    const height = 720;
    const left = Math.max(0, (this.host.screenX || 0) + ((this.host.outerWidth || 1200) - width) / 2);
    const top = Math.max(0, (this.host.screenY || 0) + ((this.host.outerHeight || 900) - height) / 2);
    const popup = this.host.open(
      "/oauth/wait", `relay-oauth-${provider}`,
      `popup=yes,width=${width},height=${height},left=${Math.round(left)},top=${Math.round(top)},resizable=yes,scrollbars=yes`,
    );
    if (!popup) throw new Error("Your browser blocked the sign-in window. Allow popups for this workspace, then click Connect again.");
    this.popup = popup;
    this.provider = provider;
    this.host.addEventListener("message", this.listener);
    this.timer = this.host.setInterval(() => {
      if (!this.popup?.closed) return;
      const provider = this.provider;
      this.dispose();
      this.onClosed(provider);
    }, 700);
    popup.focus();
    return true;
  }

  navigate(authorizationUrl) {
    const url = new URL(authorizationUrl);
    const allowedHosts = { instagram: "www.instagram.com", youtube: "accounts.google.com" };
    if (url.protocol !== "https:" || url.hostname !== allowedHosts[this.provider] || url.username || url.password) {
      throw new Error("An unexpected sign-in address was returned. Please try again.");
    }
    if (!this.popup || this.popup.closed) throw new Error("The sign-in window was closed. Click Connect to try again.");
    this.popup.location.replace(url.href);
    this.popup.focus();
  }

  receive(event) {
    if (!this.popup || event.origin !== this.host.location.origin || event.source !== this.popup) return;
    const data = event.data;
    if (!data || data.type !== "relay:oauth-result" || data.provider !== this.provider) return;
    if (!["connected", "cancelled", "connection", "expired"].includes(data.result)) return;
    this.close();
    this.onResult(data);
  }

  dispose() {
    if (this.timer !== null) this.host.clearInterval(this.timer);
    this.host.removeEventListener("message", this.listener);
    this.timer = null;
    this.popup = null;
    this.provider = null;
  }

  close() {
    if (this.popup && !this.popup.closed) this.popup.close();
    this.dispose();
  }
}
