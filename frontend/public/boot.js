// Paint the saved theme before the app bundle loads: sets the page background, CSS variables
// and the status-bar tint immediately so a returning dark-theme user never sees a flash of the
// light default (or an unthemed white status bar) while settings load over the network.
// Served as a same-origin file (the app's CSP forbids inline scripts). The cache it reads is
// written by applyTheme() in themes.ts.
(function () {
  try {
    var saved = JSON.parse(localStorage.getItem("shelf-theme") || "null");
    if (!saved || !saved.tokens) return;
    var k = saved.tokens,
      root = document.documentElement;
    if (saved.group) {
      root.setAttribute("data-theme", saved.group);
      root.style.colorScheme = saved.group;
    }
    var vars = {
      "--bg": k.bg,
      "--surface": k.surface,
      "--surface-2": k.surface2,
      "--border": k.border,
      "--text": k.text,
      "--muted": k.muted,
      "--accent": k.accent,
      "--accent-fg": k.accentFg,
    };
    for (var name in vars) if (vars[name]) root.style.setProperty(name, vars[name]);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta && k.surface) meta.setAttribute("content", k.surface);
  } catch (e) {
    /* storage unavailable — fall back to the bundle applying the theme */
  }
})();
