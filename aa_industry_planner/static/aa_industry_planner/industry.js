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
  const typeSearchCache = new Map();
  app.querySelectorAll("[data-type-search]").forEach((input, index) => {
    const wrapper = document.createElement("div");
    wrapper.className = "position-relative";
    input.replaceWith(wrapper);
    wrapper.append(input);
    const list = document.createElement("div");
    list.id = `industry-type-options-${index}`;
    list.className = "dropdown-menu w-100 p-0";
    list.style.maxHeight = "18rem";
    list.style.overflowY = "auto";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "EVE item suggestions");
    wrapper.append(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", "false");
    let timer, controller, generation = 0, results = [], active = -1;
    const hide = () => {
      list.classList.remove("show");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };
    const open = () => {
      list.classList.add("show");
      input.setAttribute("aria-expanded", "true");
    };
    const cancel = () => {
      clearTimeout(timer);
      if (controller) controller.abort();
      ++generation;
      input.removeAttribute("aria-busy");
    };
    const choose = item => {
      cancel();
      input.value = item.value;
      hide();
      input.dispatchEvent(new Event("change", {bubbles: true}));
    };
    const showMessage = message => {
      results = [];
      active = -1;
      const status = document.createElement("div");
      status.className = "dropdown-item-text py-2";
      status.setAttribute("role", "status");
      status.textContent = message;
      list.replaceChildren(status);
      open();
    };
    const showResults = items => {
      results = items;
      active = -1;
      input.removeAttribute("aria-activedescendant");
      if (!items.length) { showMessage("No matching items."); return; }
      list.replaceChildren(...items.map((item, optionIndex) => {
        const option = document.createElement("button");
        option.type = "button";
        option.tabIndex = -1;
        option.id = `${list.id}-${optionIndex}`;
        option.className = "dropdown-item text-wrap py-2";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.textContent = item.name;
        // Keep focus in the input so blur cannot close the menu before click.
        option.addEventListener("mousedown", event => event.preventDefault());
        option.addEventListener("click", () => choose(item));
        return option;
      }));
      open();
    };
    const search = () => {
      cancel();
      const requestGeneration = generation;
      const query = input.value.trim();
      hide();
      results = [];
      list.replaceChildren();
      if ((!/^\d+$/.test(query) && query.length < 2) || query.includes(" · ")) return;
      const cached = typeSearchCache.get(query);
      if (cached && cached.expires > Date.now()) {
        showResults(cached.results);
        return;
      }
      showMessage("Searching…");
      timer = setTimeout(async () => {
        controller = new AbortController();
        input.setAttribute("aria-busy", "true");
        try {
          const response = await fetch(`${app.dataset.typeSearchUrl}?q=${encodeURIComponent(query)}`, {signal: controller.signal});
          if (!response.ok) throw new Error("Search request failed");
          const data = await response.json();
          if (requestGeneration !== generation) return;
          typeSearchCache.set(query, {results: data.results, expires: Date.now() + 300000});
          if (typeSearchCache.size > 100) typeSearchCache.delete(typeSearchCache.keys().next().value);
          showResults(data.results);
        } catch (error) {
          if (requestGeneration === generation && error.name !== "AbortError") showMessage("Search unavailable. Try again or enter an EVE type ID.");
        } finally {
          if (requestGeneration === generation) input.removeAttribute("aria-busy");
        }
      }, 120);
    };
    input.addEventListener("input", search);
    input.addEventListener("focus", search);
    input.addEventListener("blur", () => { cancel(); hide(); });
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); cancel(); hide(); return; }
      if (!results.length || !list.classList.contains("show")) return;
      if (event.key === "Enter") {
        event.preventDefault();
        choose(results[active < 0 ? 0 : active]);
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        active = event.key === "ArrowDown" ? (active + 1) % results.length : (active <= 0 ? results.length : active) - 1;
        [...list.children].forEach((option, optionIndex) => {
          option.classList.toggle("active", optionIndex === active);
          option.setAttribute("aria-selected", String(optionIndex === active));
        });
        input.setAttribute("aria-activedescendant", list.children[active].id);
        list.children[active].scrollIntoView({block: "nearest"});
      }
    });
  });
  app.querySelectorAll("[data-industry-assignment]").forEach(form => {
    const activity = form.querySelector("[data-assignment-activity]");
    const selects = [...form.querySelectorAll("[data-assignment-family], [data-assignment-service]")];
    const update = () => selects.forEach(select => {
      [...select.options].forEach(option => {
        const mismatch = Boolean(activity.value && option.dataset.activity && option.dataset.activity !== activity.value);
        option.hidden = mismatch;
        option.disabled = mismatch;
      });
      if (select.selectedOptions[0]?.disabled) select.value = "";
      select.querySelectorAll("optgroup").forEach(group => {
        group.hidden = [...group.children].every(option => option.hidden);
      });
    });
    activity.addEventListener("change", update);
    update();
  });
  app.querySelectorAll("[data-type-icon]").forEach(image => {
    const hide = () => { image.hidden = true; };
    image.addEventListener("error", hide);
    if (image.complete && !image.naturalWidth) hide();
  });
  app.querySelectorAll("[data-expand-tree]").forEach(button => {
    const details = [...app.querySelectorAll(".aip-tree details")];
    const update = () => { button.textContent = details.every(node => node.open) ? "Collapse all" : "Expand all"; };
    update();
    button.addEventListener("click", () => {
      const expanded = details.every(node => node.open);
      details.forEach(node => { node.open = !expanded; });
      update();
    });
    details.forEach(node => node.addEventListener("toggle", update));
  });
  app.querySelectorAll("[data-tree-top]").forEach(button => button.addEventListener("click", () => {
    app.querySelectorAll(".aip-tree details").forEach(node => { node.open = node.dataset.treeDepth === "0"; });
  }));
  app.querySelectorAll("[data-tree-reference]").forEach(link => link.addEventListener("click", event => {
    const target = document.getElementById(link.hash.slice(1));
    if (!target) return;
    event.preventDefault();
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
    target.scrollIntoView({behavior: "smooth", block: "center"});
    (target.querySelector("summary") || target).focus({preventScroll: true});
  }));
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
