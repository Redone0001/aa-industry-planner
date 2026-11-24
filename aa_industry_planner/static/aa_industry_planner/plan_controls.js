(() => {
  "use strict";
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
    textarea.dataset.fullText ??= textarea.value;
    let lines = textarea.dataset.fullText.split("\n").filter(Boolean);
    if (button.dataset.copyMultibuy === "unchecked") {
      const excluded = new Set([...section.querySelectorAll('tr')].filter(row => row.querySelector('input[name="checked"]')?.checked).map(row => row.cells[0].textContent.trim()));
      lines = lines.filter(line => !excluded.has(line.replace(/ \d+$/, "")));
    }
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
