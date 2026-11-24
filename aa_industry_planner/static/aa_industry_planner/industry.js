(() => {
  "use strict";
  const app = document.querySelector(".aip");
  if (!app) return;
  const navLinks = [...app.querySelectorAll(".aip-nav a")];
  const overviewPath = new URL(navLinks[0].href).pathname;
  navLinks.forEach(link => {
    const path = new URL(link.href).pathname;
    const active = location.pathname === path || (path !== overviewPath && location.pathname.startsWith(path));
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  app.querySelectorAll("[data-type-search]").forEach((input, index) => {
    const list = document.createElement("datalist");
    list.id = `industry-type-options-${index}`;
    input.setAttribute("list", list.id);
    input.insertAdjacentElement("afterend", list);
    let timer, controller;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      if (controller) controller.abort();
      if (input.value.length < 2 || input.value.includes(" · ")) return;
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch(`${app.dataset.typeSearchUrl}?q=${encodeURIComponent(input.value)}`, {signal: controller.signal});
          if (!response.ok) return;
          const data = await response.json();
          list.replaceChildren(...data.results.map(item => {
            const option = document.createElement("option");
            option.value = item.value;
            return option;
          }));
        } catch (error) {
          if (error.name !== "AbortError") input.setAttribute("placeholder", "Search unavailable; enter an EVE type ID");
        }
      }, 200);
    });
  });
  app.querySelectorAll("[data-expand-tree]").forEach(button => {
    let expanded = true;
    button.textContent = "Collapse all";
    button.addEventListener("click", () => {
      expanded = !expanded;
      app.querySelectorAll(".aip-tree details").forEach(node => { node.open = expanded; });
      button.textContent = expanded ? "Collapse all" : "Expand all";
    });
  });
  app.querySelectorAll("[data-index-history]").forEach(async container => {
    const status = container.querySelector(".aip-chart-status");
    try {
      const response = await fetch(container.dataset.indexHistory);
      if (!response.ok) throw new Error("History request failed");
      const data = await response.json();
      if (!data.samples.length) { status.textContent = "History will appear after the first hourly snapshot."; return; }
      const groups = new Map();
      for (const sample of data.samples) {
        if (!groups.has(sample.activity)) groups.set(sample.activity, []);
        groups.get(sample.activity).push({x: Date.parse(sample.recorded_at), y: Number(sample.cost_index) * 100});
      }
      const entries = [...groups.entries()];
      entries.forEach(([name]) => {
        const label = document.createElement("span");
        const swatch = document.createElement("span");
        swatch.className = "aip-chart-swatch";
        swatch.setAttribute("aria-hidden", "true");
        label.append(swatch, document.createTextNode(name.replaceAll("_", " ")));
        container.querySelector(".aip-chart-legend").append(label);
      });
      const canvas = container.querySelector("canvas");
      const draw = () => {
        const theme = getComputedStyle(container);
        const color = token => theme.getPropertyValue(token).trim() || theme.color;
        const colors = ["--bs-success", "--bs-info", "--bs-warning", "--bs-danger", "--bs-primary", "--bs-secondary"].map(color);
        container.querySelectorAll(".aip-chart-swatch").forEach((swatch, index) => {
          swatch.style.backgroundColor = colors[index % colors.length];
        });
        const width = Math.max(280, container.clientWidth), height = 240, ratio = devicePixelRatio || 1;
        canvas.width = width * ratio; canvas.height = height * ratio;
        const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
        const values = entries.flatMap(([, points]) => points);
        const xmin = Math.min(...values.map(p => p.x)), xmax = Math.max(xmin + 3600000, ...values.map(p => p.x));
        const ymax = Math.max(0.1, ...values.map(p => p.y)) * 1.1;
        const x = value => 48 + (value - xmin) / (xmax - xmin) * (width - 64);
        const y = value => 207 - value / ymax * 185;
        ctx.font = `12px ${theme.fontFamily}`;
        for (let line = 0; line <= 4; line++) {
          const value = ymax / 4 * line;
          ctx.strokeStyle = color("--bs-border-color"); ctx.beginPath(); ctx.moveTo(48, y(value)); ctx.lineTo(width - 16, y(value)); ctx.stroke();
          ctx.fillStyle = color("--bs-secondary-color"); ctx.textAlign = "right"; ctx.fillText(`${value.toFixed(1)}%`, 41, y(value) + 4);
        }
        entries.forEach(([, points], index) => {
          ctx.strokeStyle = colors[index % colors.length]; ctx.lineWidth = 2; ctx.beginPath();
          points.forEach((point, i) => i ? ctx.lineTo(x(point.x), y(point.y)) : ctx.moveTo(x(point.x), y(point.y)));
          ctx.stroke();
          if (points.length === 1) { ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.arc(x(points[0].x), y(points[0].y), 3, 0, Math.PI * 2); ctx.fill(); }
        });
        ctx.fillStyle = color("--bs-secondary-color"); ctx.textAlign = "left"; ctx.fillText(new Date(xmin).toLocaleDateString(), 48, 232);
        ctx.textAlign = "right"; ctx.fillText(new Date(xmax).toLocaleDateString(), width - 16, 232);
      };
      draw();
      if (typeof ResizeObserver !== "undefined") new ResizeObserver(draw).observe(container);
      new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme", "data-bs-theme"]});
      status.textContent = `${data.samples.length.toLocaleString()} hourly samples · ${data.system} · 90-day retention`;
    } catch (error) { status.textContent = "Could not load index history. Please reload the page."; }
  });
})();
