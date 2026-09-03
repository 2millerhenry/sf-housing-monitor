(() => {
  const form = document.querySelector("[data-deal-form]");
  if (!form) return;

  const syncPath = (toggle) => {
    const panel = form.querySelector(`[data-path-fields="${toggle.dataset.pathToggle}"]`);
    if (!panel) return;
    const card = form.querySelector(`[data-path-card="${toggle.dataset.pathToggle}"]`);
    card?.classList.toggle("is-selected", toggle.checked);
    panel.querySelectorAll("input, select").forEach((field) => {
      field.disabled = !toggle.checked;
      if (field.matches("[data-path-required]")) field.required = toggle.checked;
    });
  };
  form.querySelectorAll("[data-path-toggle]").forEach((toggle) => {
    toggle.addEventListener("change", () => syncPath(toggle));
    syncPath(toggle);
  });

  const anywhere = form.querySelector("[data-anywhere-toggle]");
  const areaPicker = form.querySelector("[data-area-picker]");
  const areaTiers = [...form.querySelectorAll("[data-area-tier]")];
  const selectedAreaInputs = () => [...form.querySelectorAll("[data-area-value]")];
  const selectedAreas = () => new Set(selectedAreaInputs().map((input) => input.value));

  const announceAreaChange = () => {
    form.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const syncAreaOptions = () => {
    const allSelected = selectedAreas();
    areaTiers.forEach((tier) => {
      const select = tier.querySelector("[data-area-add]");
      const count = tier.querySelector("[data-area-count]");
      const own = new Set([...tier.querySelectorAll("[data-area-value]")].map((input) => input.value));
      const limit = Number(tier.dataset.areaLimit) || 5;
      const full = own.size >= limit;
      if (count) count.textContent = `${own.size} of ${limit}`;
      if (!select) return;
      select.disabled = Boolean(anywhere?.checked) || full;
      [...select.options].forEach((option) => {
        if (!option.value) return;
        option.disabled = full || (allSelected.has(option.value) && !own.has(option.value));
      });
    });
  };

  const removeArea = (event) => {
    const chip = event.currentTarget.closest(".area-chip");
    chip?.remove();
    syncAreaOptions();
    announceAreaChange();
  };

  const addArea = (event) => {
    const select = event.currentTarget;
    const area = select.value;
    const tier = select.closest("[data-area-tier]");
    if (!area || !tier) return;
    const limit = Number(tier.dataset.areaLimit) || 5;
    const own = [...tier.querySelectorAll("[data-area-value]")];
    if (own.length >= limit || selectedAreas().has(area)) {
      select.value = "";
      return;
    }
    const chip = document.createElement("li");
    chip.className = "area-chip";
    const label = document.createElement("span");
    label.textContent = area;
    const value = document.createElement("input");
    value.type = "hidden";
    value.name = `areas_${tier.dataset.areaTier}`;
    value.value = area;
    value.dataset.areaValue = "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "area-remove";
    remove.dataset.areaRemove = "";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${area}`);
    remove.addEventListener("click", removeArea);
    chip.append(label, value, remove);
    tier.querySelector("[data-area-selected]")?.append(chip);
    select.value = "";
    syncAreaOptions();
    announceAreaChange();
  };

  areaTiers.forEach((tier) => {
    tier.querySelector("[data-area-add]")?.addEventListener("change", addArea);
    tier.querySelectorAll("[data-area-remove]").forEach((button) => button.addEventListener("click", removeArea));
  });

  const syncAreas = () => {
    if (!anywhere || !areaPicker) return;
    areaPicker.hidden = anywhere.checked;
    areaPicker.querySelectorAll("select, input, button").forEach((field) => { field.disabled = anywhere.checked; });
    if (!anywhere.checked) syncAreaOptions();
  };
  if (anywhere) anywhere.addEventListener("change", syncAreas);
  syncAreas();

  const flexible = form.querySelector("[data-flexible-toggle]");
  const dateFields = form.querySelector("[data-date-fields]");
  const syncDates = () => {
    if (!flexible || !dateFields) return;
    dateFields.hidden = flexible.checked;
    dateFields.querySelectorAll("input").forEach((field) => { field.disabled = flexible.checked; });
  };
  if (flexible) flexible.addEventListener("change", syncDates);
  syncDates();

  form.querySelectorAll("[data-split-total]").forEach((output) => {
    const key = output.dataset.splitTotal;
    const maximum = form.elements[`${key}_maximum`];
    const occupants = form.elements[`${key}_occupants`];
    const update = () => {
      const each = Number(maximum.value) || 0;
      const people = Number(occupants.value) || 0;
      output.textContent = each && people
        ? `$${(each * people).toLocaleString()} total ($${each.toLocaleString()} each for ${people}).`
        : "The total maximum is calculated from the per-person limit and group size.";
    };
    maximum.addEventListener("input", update);
    occupants.addEventListener("change", update);
    update();
  });

  const draftStatus = form.querySelector("[data-draft-status]");
  let draftTimer;
  const saveDraft = async () => {
    if (form.dataset.draftAutosave !== "1") return;
    if (draftStatus) draftStatus.textContent = "Saving your choices…";
    try {
      const response = await fetch("/preferences/draft", {
        method: "POST",
        body: new FormData(form),
        headers: { "X-SF-Housing-Request": "browser" },
      });
      if (draftStatus) draftStatus.textContent = response.ok
        ? "Your choices are saved on this Mac."
        : "Keep going; the complete choices will save when valid.";
    } catch (_) {
      if (draftStatus) draftStatus.textContent = "Draft save is waiting for the monitor to reconnect.";
    }
  };
  if (form.dataset.draftAutosave === "1") {
    form.addEventListener("input", () => {
      window.clearTimeout(draftTimer);
      draftTimer = window.setTimeout(saveDraft, 450);
    });
    form.addEventListener("change", () => {
      window.clearTimeout(draftTimer);
      draftTimer = window.setTimeout(saveDraft, 150);
    });
  }

  const submitButton = form.querySelector("[data-deal-submit]");
  const submitLabel = form.querySelector("[data-deal-submit-label]");
  const submitStatus = form.querySelector("[data-submit-status]");
  form.addEventListener("submit", (event) => {
    const first = form.querySelector("[data-path-toggle]");
    if (!form.querySelector("[data-path-toggle]:checked")) {
      event.preventDefault();
      first.setCustomValidity("Choose at least one kind of home.");
      first.reportValidity();
      first.addEventListener("change", () => first.setCustomValidity(""), { once: true });
    } else if (!form.checkValidity()) {
      event.preventDefault();
      form.reportValidity();
    } else if (submitButton) {
      const firstActivation = submitButton.dataset.firstActivation === "true";
      submitButton.disabled = true;
      submitButton.dataset.submitting = "true";
      submitButton.setAttribute("aria-busy", "true");
      if (submitLabel) submitLabel.textContent = firstActivation
        ? "Saving deal and starting your first check…"
        : "Saving and reranking homes…";
      if (submitStatus) submitStatus.textContent = firstActivation
        ? "Your search is being saved. The shortlist will show live check progress next."
        : "Your deal is being saved and every stored home is being reranked.";
    }
  });
})();
