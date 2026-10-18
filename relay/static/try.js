"use strict";
const caption = document.querySelector("#preview-caption");
const save = document.querySelector("#preview-save");
try { caption.value = JSON.parse(localStorage.getItem("relay:preview-draft"))?.caption || ""; } catch {}
function updatePreview() {
  document.querySelector("#preview-copy").textContent = caption.value || "Your words will appear here.";
  try {
    localStorage.setItem("relay:preview-draft", JSON.stringify({ caption: caption.value, savedAt: Date.now(), selectedAccountIds: [] }));
    save.textContent = `${caption.value.length}/2,200 characters · saved in this browser`;
  } catch {
    document.querySelector("#preview-error").hidden = false;
    document.querySelector("#preview-error").textContent = "Browser storage is unavailable. Keep this tab open and copy your caption before creating an account.";
    save.textContent = "Caption kept in this window.";
  }
}
caption.addEventListener("input", updatePreview);
updatePreview();
