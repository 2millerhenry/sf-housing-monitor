(() => {
  const button = document.querySelector("[data-copy-report]");
  const result = document.querySelector("[data-copy-result]");
  if (!button || !result) return;

  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch("/support/report.json", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("report unavailable");
      const report = JSON.stringify(await response.json(), null, 2);
      await navigator.clipboard.writeText(report);
      result.textContent = "Redacted support report copied.";
      result.classList.remove("error");
    } catch (_) {
      result.textContent = "The report could not be copied. Run the check again and retry.";
      result.classList.add("error");
    } finally {
      result.hidden = false;
      button.disabled = false;
    }
  });
})();
