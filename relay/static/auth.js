"use strict";

const signup = window.location.pathname === "/signup";
const $ = (selector) => document.querySelector(selector);
if (signup) {
  document.title = "Create an account · Relay";
  $("#auth-title").textContent = "Start your next chapter.";
  $("#account-submit").textContent = "Create account";
  $("#switch-prompt").textContent = "Already have an account?";
  $("#switch-link").textContent = "Log in";
  $("#switch-link").href = "/login";
  $("#password").minLength = 12;
  $("#password").autocomplete = "new-password";
  $("#password").placeholder = "At least 12 characters";
  document.querySelectorAll("[data-signup]").forEach((node) => {
    node.hidden = false;
    node.querySelectorAll("input").forEach((input) => { input.disabled = false; input.required = true; });
  });
}

$("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#account-submit");
  const error = $("#account-error");
  button.disabled = true;
  error.hidden = true;
  button.textContent = signup ? "Creating your account…" : "Logging in…";
  try {
    const response = await fetch(signup ? "/auth/signup" : "/auth/login", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(event.target))),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
      throw new Error(detail || "Check your details and try again.");
    }
    window.location.replace("/app");
  } catch (failure) {
    error.replaceChildren();
    const message = document.createElement("span");
    message.textContent = failure instanceof TypeError
      ? "Couldn’t reach Relay. Your details are still here."
      : failure.message;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Retry now";
    retry.addEventListener("click", () => $("#account-form").requestSubmit());
    error.append(message, retry);
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = signup ? "Create account" : "Log in";
  }
});
