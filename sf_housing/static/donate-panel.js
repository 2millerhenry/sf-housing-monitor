(() => {
  const link = document.querySelector("[data-donate-open]");
  const dialog = document.querySelector("[data-donate-dialog]");
  const frame = dialog?.querySelector("[data-donate-frame]");
  if (!link || !dialog || !frame || typeof dialog.showModal !== "function") return;

  // Nothing is fetched from Ko-fi until somebody asks for it. Opening the
  // dashboard has to tell them nothing at all.
  const load = () => {
    if (frame.getAttribute("src") === "about:blank") {
      frame.setAttribute("src", link.dataset.donateEmbed);
    }
  };

  link.addEventListener("click", (event) => {
    // A modified click is a deliberate "open this somewhere else".
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    load();
    dialog.showModal();
  });

  dialog.addEventListener("click", (event) => {
    // The backdrop is the dialog itself; a click on the panel is not a click out.
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector("[data-donate-close]")?.addEventListener("click", () => dialog.close());
})();
