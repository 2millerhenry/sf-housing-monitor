// Opening a listing and copying a contact note behave the same wherever a
// listing is shown. This used to live inline in the results table, so a page
// that showed one listing on its own had the markup and none of the behaviour.
document.addEventListener("click", function (event) {
  const link = event.target.closest("a[data-mark-opened]");
  if (link) {
    const card = link.closest(".listing-row, .listing-card");
    if (card) card.classList.add("opened-listing");
    fetch(link.dataset.markOpened, { method: "POST", credentials: "same-origin", keepalive: true }).catch(function () {});
  }
  const copyButton = event.target.closest("button[data-copy-outreach]");
  if (!copyButton) return;
  const field = document.getElementById(copyButton.dataset.copyOutreach);
  if (!field) return;
  field.select();
  const copied = navigator.clipboard && navigator.clipboard.writeText
    ? navigator.clipboard.writeText(field.value)
    : Promise.reject();
  copied.catch(function () { document.execCommand("copy"); }).finally(function () {
    copyButton.textContent = "Copied";
    window.setTimeout(function () { copyButton.textContent = "Copy note"; }, 1300);
  });
});
