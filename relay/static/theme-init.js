// Applies the resolved theme before first paint. This must stay a blocking
// classic script in <head>: deferring it would paint the default theme first and
// then flip, which is the flash the inline version existed to avoid. It is
// external so the application can run under a strict script-src 'self' policy
// with no inline-script allowance.
//
// Precedence: an explicit stored choice wins; otherwise the operating system
// decides. Writing the resolved value to data-theme means the CSS never has to
// guess, and a later OS change is picked up live only while no choice is stored.
(function () {
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try {
      var value = localStorage.getItem("relay-theme");
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null;
    }
  }

  function resolve() {
    return stored() || (media && media.matches ? "dark" : "light");
  }

  function apply(theme) {
    root.dataset.theme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#141a16" : "#f3f0e7");
  }

  apply(resolve());

  // Follow the OS while the reader has not chosen a theme themselves.
  if (media) {
    var onChange = function () {
      if (!stored()) apply(media.matches ? "dark" : "light");
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else if (media.addListener) media.addListener(onChange);
  }

  // commands.js calls this after a toggle so the meta theme-colour stays honest.
  window.relayTheme = { apply: apply, stored: stored };
})();
