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
  let searchIndex = 0;
  const attachItemSearch = input => {
    const wrapper = document.createElement("div");
    wrapper.className = "position-relative";
    input.replaceWith(wrapper);
    wrapper.append(input);
    const list = document.createElement("div");
    list.id = `industry-type-options-${searchIndex++}`;
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
  };
  app.querySelectorAll("[data-type-search]").forEach(attachItemSearch);
  app.querySelectorAll("[data-plan-targets]").forEach(form => {
    const rows = form.querySelector("[data-target-rows]");
    const template = form.querySelector("[data-target-template]");
    const add = form.querySelector("[data-add-target]");
    const total = form.elements["targets-TOTAL_FORMS"];
    const limit = Number(form.elements["targets-MAX_NUM_FORMS"].value || 50);
    const visibleRows = () => [...rows.children].filter(row => !row.hidden);
    const update = () => {
      const visible = visibleRows();
      add.disabled = visible.length >= limit;
      visible.forEach(row => {
        row.querySelector("[data-remove-target]").hidden = visible.length === 1;
      });
    };
    add.hidden = false;
    add.addEventListener("click", () => {
      const spare = [...rows.children].find(row => row.hidden);
      const index = spare ? spare.querySelector("[name$='-DELETE']").name.split("-")[1] : total.value;
      const fields = document.createElement("template");
      fields.innerHTML = template.innerHTML.replaceAll("__prefix__", index);
      const row = fields.content.firstElementChild;
      if (spare) spare.replaceWith(row);
      else {
        rows.append(row);
        total.value = Number(total.value) + 1;
      }
      const input = row.querySelector("[data-type-search]");
      attachItemSearch(input);
      update();
      input.focus();
    });
    rows.addEventListener("click", event => {
      const remove = event.target.closest("[data-remove-target]");
      if (!remove) return;
      const row = remove.closest("[data-target-row]");
      row.querySelector("[name$='-DELETE']").value = "on";
      row.hidden = true;
      update();
      add.focus();
    });
    update();
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
})();
