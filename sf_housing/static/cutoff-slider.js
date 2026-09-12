// The shortlist cut-off. Dragging changes a number, so the number says what it
// would cost: how many homes are at or above it right now. Without this script
// the range input still submits the right value; only the live readout is lost.
(function () {
  const panel = document.querySelector("[data-cutoff]");
  if (!panel) return;
  const slider = panel.querySelector(".cutoff-slider");
  const value = panel.querySelector(".cutoff-value");
  const count = panel.querySelector(".cutoff-count");
  if (!slider || !value || !count) return;

  let counts = {};
  try {
    counts = JSON.parse(slider.dataset.cutoffCounts || "{}");
  } catch (error) {
    counts = {};
  }
  // The page is rendered with the counts for the deal as saved. While the deal
  // is being edited the form sends replacements, which are for a deal that has
  // not been saved and may be measured from a sample rather than the whole
  // pool -- so the readout has to be able to say "about".
  let approximate = false;
  // Nothing collected yet means nothing to count. Saying "0 homes" while
  // somebody is still writing their deal reads as a verdict on the deal, when
  // the first search simply has not run.
  let pool = Number(slider.dataset.cutoffPool || 0);
  // Nothing stored means the number is what this kind of search usually finds
  // rather than what is on the board, so it is offered as "about".
  if (pool === 0) approximate = true;

  function render() {
    const current = Number(slider.value);
    const min = Number(slider.min);
    const max = Number(slider.max);
    const fill = max > min ? ((current - min) / (max - min)) * 100 : 0;
    panel.style.setProperty("--cutoff-fill", fill.toFixed(2) + "%");
    value.textContent = String(current);
    const homes = counts[String(current)];
    // Nought is the one number worth saying nothing about: it is what an empty
    // database returns and what a deal nobody can estimate returns, and
    // neither means "your deal finds nothing".
    if (homes === undefined || !homes) {
      count.textContent = "and up";
      return;
    }
    const noun = homes === 1 ? " home" : " homes";
    count.textContent = "and up · " + (approximate ? "about " : "") + homes + noun;
  }

  // Sent by the deal form each time it has measured the deal on screen.
  document.addEventListener("cutoff-counts", (event) => {
    const next = event.detail && event.detail.counts;
    if (!next) return;
    counts = next;
    approximate = Boolean(event.detail.approximate);
    if (typeof event.detail.pool === "number") pool = event.detail.pool;
    if (event.detail.fromMarket) approximate = true;
    render();
  });

  slider.addEventListener("input", render);
  render();
})();
