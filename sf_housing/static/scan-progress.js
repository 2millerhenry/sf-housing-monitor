(() => {
  const root = document.querySelector("[data-scan-progress]");
  if (!root) return;

  const title = root.querySelector("[data-scan-title]");
  const percentLabel = root.querySelector("[data-scan-percent]");
  const track = root.querySelector("[role='progressbar']");
  const bar = root.querySelector("[data-scan-bar]");
  const detail = root.querySelector("[data-scan-detail]");
  const estimate = root.querySelector("[data-scan-estimate]");
  let finished = false;

  const update = (progress) => {
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    const sourceCount = Number(progress.sources_total) || 0;
    const completed = Number(progress.sources_completed) || 0;
    const elapsed = Number(progress.elapsed_seconds) || 0;
    const checked = Number(progress.listings_seen) || 0;

    title.textContent = progress.current_source
      ? `Checking ${progress.current_source}`
      : "Starting source checks";
    percentLabel.textContent = `${percent}%`;
    track.setAttribute("aria-valuenow", String(percent));
    bar.style.setProperty("--scan-progress", String(percent / 100));
    detail.textContent = `${elapsed}s elapsed · ${completed} of ${sourceCount} sources · ${checked} listings checked`;
    estimate.textContent = progress.typical_seconds
      ? `Usually about ${progress.typical_seconds}s`
      : `Up to ${progress.maximum_seconds || 120}s`;

    if (!progress.running && !finished) {
      finished = true;
      const hasErrors = progress.status === "completed_with_errors";
      title.textContent = progress.status === "failed"
        ? "Check stopped before it could finish"
        : hasErrors
          ? "Check finished with a source error"
          : "Check complete";
      if (progress.status === "completed" || hasErrors) {
        percentLabel.textContent = "100%";
        track.setAttribute("aria-valuenow", "100");
        bar.style.setProperty("--scan-progress", "1");
      }
      window.setTimeout(() => {
        const destination = new URL(window.location.href);
        destination.searchParams.delete("message");
        destination.searchParams.delete("scan");
        window.location.replace(destination);
      }, 1400);
    }
  };

  const poll = async () => {
    try {
      const response = await fetch("/scan/status", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`Progress request failed: ${response.status}`);
      update(await response.json());
    } catch (_error) {
      title.textContent = "Still checking sources";
    }
    if (!finished) window.setTimeout(poll, 500);
  };

  poll();
})();
