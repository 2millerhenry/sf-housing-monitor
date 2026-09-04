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

  function render() {
    const current = Number(slider.value);
    const min = Number(slider.min);
    const max = Number(slider.max);
    const fill = max > min ? ((current - min) / (max - min)) * 100 : 0;
    panel.style.setProperty("--cutoff-fill", fill.toFixed(2) + "%");
    value.textContent = String(current);
    const homes = counts[String(current)];
    // A count we do not have is left unsaid rather than shown as zero.
    count.textContent =
      homes === undefined ? "and up" : "and up · " + homes + (homes === 1 ? " home" : " homes");
  }

  slider.addEventListener("input", render);
  render();
})();
