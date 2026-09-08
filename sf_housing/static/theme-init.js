// Loaded from <head> without defer, so it runs before the first paint and a
// dark reader never watches the page load light and then turn over. It is a
// file rather than an inline script because no page here carries inline
// script: that rule is what makes the pages safe to serve whatever a source
// wrote, and a theme is not worth an exception to it.
(function () {
  try {
    var saved = localStorage.getItem("sf-theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    // A browser with site data blocked still gets a theme, just not a memory.
  }
})();
