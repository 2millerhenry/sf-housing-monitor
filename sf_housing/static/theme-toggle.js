// The stored choice is applied by a tiny script in <head>, before anything is
// painted, so the page never flashes the wrong theme. This file only handles
// the button.
(() => {
  const KEY = "sf-theme";
  const root = document.documentElement;
  const button = document.querySelector("[data-theme-toggle]");
  if (!button) return;

  const system = window.matchMedia("(prefers-color-scheme: dark)");
  // No stored choice means the Mac is still deciding, and the CSS is already
  // following it. The button has to report that same answer or it will claim
  // to be off while the page is dark.
  const isDark = () => (root.dataset.theme ? root.dataset.theme === "dark" : system.matches);
  const sync = () => button.setAttribute("aria-pressed", String(isDark()));

  button.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.dataset.theme = next;
    try {
      localStorage.setItem(KEY, next);
    } catch (_) {
      // A locked-down browser still gets the theme, just not the memory of it.
    }
    sync();
  });

  system.addEventListener("change", () => {
    if (!root.dataset.theme) sync();
  });
  sync();
})();
