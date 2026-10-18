import { OAuthPopup } from "/static/oauth-popup.mjs";

window.oauthPopup = new OAuthPopup(window, window.handleOAuthResult, window.handleOAuthClosed);
window.initialize();
