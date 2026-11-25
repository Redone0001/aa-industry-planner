(() => {
  "use strict";
  let applyStepFilter = () => {};
  const sequence = document.querySelector("[data-step-sequence]");
  if (sequence) {
    const steps = [...sequence.querySelectorAll(".aip-step")];
    const select = sequence.querySelector("[data-step-filter]");
    const count = sequence.querySelector("[data-step-filter-count]");
    const empty = sequence.querySelector("[data-step-filter-empty]");
    const filters = {
      all: () => true,
      remaining: step => Number(step.dataset.progress) < 100,
      ready: step => step.dataset.state === "ready",
      checked: step => step.classList.contains("is-reviewed"),
    };
    applyStepFilter = () => {
      const matches = filters[select.value] || filters.all;
      let shown = 0;
      steps.forEach(step => {
        step.hidden = !matches(step);
        if (!step.hidden) shown++;
        else if (step.contains(document.activeElement)) select.focus({preventScroll: true});
      });
      count.textContent = `${shown} of ${steps.length} steps · inputs first`;
      empty.hidden = !steps.length || shown > 0;
    };
    const revealStep = hash => {
      const step = steps.find(item => `#${item.id}` === hash);
      if (step?.hidden) { select.value = "all"; applyStepFilter(); }
      if (step?.matches("[data-step-collapse]")) step.open = true;
      return step;
    };
    select.addEventListener("change", applyStepFilter);
    sequence.querySelector("[data-step-filter-reset]").addEventListener("click", () => {
      select.value = "all"; applyStepFilter(); select.focus();
    });
    document.addEventListener("click", event => {
      if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target.closest("a[href]");
      if (!link) return;
      const url = new URL(link.href, location.href);
      if (url.origin === location.origin && url.pathname === location.pathname && url.search === location.search) revealStep(url.hash);
    });
    window.addEventListener("hashchange", () => {
      const step = revealStep(location.hash);
      if (step) step.scrollIntoView({block: "start"});
    });
    sequence.querySelector("[data-step-filter-controls]").hidden = !steps.length;
    applyStepFilter();
    revealStep(location.hash);
  }
  const feeBoxes = [...document.querySelectorAll("[data-fee]")];
  const positionFee = box => {
    const trigger = box.querySelector("button");
    const popup = box.querySelector(".aip-fee-popup");
    const rect = trigger.getBoundingClientRect();
    const left = rect.left >= popup.offsetWidth + 12 ? rect.left - popup.offsetWidth : Math.max(12, Math.min(rect.right - popup.offsetWidth, window.innerWidth - popup.offsetWidth - 12));
    popup.style.left = `${left}px`;
    popup.style.right = "auto";
    popup.style.top = `${Math.max(80, Math.min(rect.bottom, window.innerHeight - popup.offsetHeight - 12))}px`;
  };
  feeBoxes.forEach(box => {
    const trigger = box.querySelector("button");
    const show = () => { box.classList.remove("is-fee-dismissed"); positionFee(box); };
    box.addEventListener("mouseenter", show);
    box.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); });
    trigger.addEventListener("focus", show);
    trigger.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); show(); });
    box.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); box.classList.add("is-fee-dismissed"); }
    });
  });
  if (feeBoxes.length) {
    const reposition = () => feeBoxes.forEach(box => { if (box.matches(":hover, :focus-within")) positionFee(box); });
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, {passive: true, capture: true});
  }
  document.addEventListener("click", event => {
    document.querySelectorAll("[data-fee]").forEach(box => { if (!box.contains(event.target)) box.classList.add("is-fee-dismissed"); });
  });
  document.addEventListener("change", async event => {
    const checkbox = event.target.closest('[data-checklist] input[name="checked"]');
    if (!checkbox) return;
    const form = checkbox.closest("form");
    const id = form.dataset.itemId;
    const feedback = form.querySelector('[role="status"]');
    const controls = [...document.querySelectorAll(`[data-checklist][data-item-id="${id}"] input[name="checked"]`)];
    const checked = checkbox.checked;
    const body = new FormData(form);
    controls.forEach(input => { input.disabled = true; });
    feedback.textContent = "Saving…";
    try {
      const response = await fetch(form.action, {method: "POST", body, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error("Save failed");
      const data = await response.json();
      controls.forEach(input => { input.checked = data.checked; });
      document.querySelectorAll(`[data-review-item="${id}"]`).forEach(item => item.classList.toggle("is-reviewed", data.checked));
      feedback.textContent = "Saved";
      applyStepFilter();
    } catch (_) {
      controls.forEach(input => { input.checked = !checked; });
      feedback.textContent = "Could not save. Try again.";
    } finally { controls.forEach(input => { input.disabled = false; }); }
  });
  document.querySelectorAll('[data-checklist-save]').forEach(button => { button.hidden = true; });
  document.querySelectorAll('[data-checklist]').forEach(form => form.addEventListener("click", event => event.stopPropagation()));
  document.querySelectorAll('[data-copy-multibuy]').forEach(button => button.addEventListener("click", async () => {
    const section = button.closest('[data-buy-list]');
    const textarea = section.querySelector('[data-multibuy-text]');
    const status = section.querySelector('[data-copy-status]');
    const filter = button.dataset.copyMultibuy;
    const lines = [...section.querySelectorAll('[data-buy-line]')].filter(row => {
      if (filter === "unchecked") return !row.classList.contains("is-reviewed");
      return !filter || row.dataset.buyCategory === filter;
    }).map(row => row.dataset.buyLine);
    const text = lines.join("\n");
    if (!text) { status.textContent = "No items to copy."; return; }
    try {
      await navigator.clipboard.writeText(text);
      status.textContent = `Copied ${lines.length} item${lines.length === 1 ? "" : "s"}. Paste into EVE Multibuy.`;
    } catch (_) {
      textarea.value = text;
      textarea.closest('details').open = true;
      textarea.focus(); textarea.select();
      status.textContent = "Clipboard unavailable. Select and copy the multibuy text below.";
    }
  }));
})();
