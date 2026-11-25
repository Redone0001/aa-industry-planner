(() => {
  "use strict";
  const panel = document.querySelector("[data-plan-overview]");
  const payload = document.getElementById("industry-dependency-graph");
  if (!panel || !payload) return;
  const data = JSON.parse(payload.textContent);
  const nodes = new Map(data.nodes.map(node => [node.id, node]));
  const graphPane = panel.querySelector("[data-graph-pane]");
  const treePane = panel.querySelector("[data-tree-pane]");
  const viewport = panel.querySelector("[data-graph-viewport]");
  const world = panel.querySelector("[data-graph-world]");
  const inspector = panel.querySelector("[data-graph-inspector]");
  const scaleLabel = panel.querySelector("[data-graph-scale]");
  const nodeElements = new Map();
  const edgeElements = [];
  const formatter = new Intl.NumberFormat(document.documentElement.lang || "en");
  const number = value => formatter.format(value);
  const edgeAmounts = edge => [
    edge.quantity ? `${number(edge.quantity)} needed` : "",
    edge.covered_quantity ? `${number(edge.covered_quantity)} covered by purchase` : "",
  ].filter(Boolean).join(" · ");
  const element = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const icon = id => {
    const box = element("span", "aip-item-icon");
    box.setAttribute("aria-hidden", "true");
    box.append(element("i", "fa-solid fa-cube"));
    const image = element("img");
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("error", () => { image.hidden = true; });
    image.src = `https://images.evetech.net/types/${id}/icon?size=64`;
    box.append(image);
    return box;
  };
  let selected = null;
  let camera = {x: 0, y: 0, scale: 1};
  let initialized = false;
  const apply = () => {
    world.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})`;
    scaleLabel.value = `${Math.round(camera.scale * 100)}%`;
    scaleLabel.textContent = scaleLabel.value;
  };
  const center = node => {
    camera.x = viewport.clientWidth / 2 - (node.x + node.width / 2) * camera.scale;
    camera.y = viewport.clientHeight / 2 - (node.y + node.height / 2) * camera.scale;
    apply();
  };
  const fit = () => {
    if (!viewport.clientWidth || !viewport.clientHeight) return;
    camera.scale = Math.max(0.001, Math.min(1, (viewport.clientWidth - 24) / data.width, (viewport.clientHeight - 24) / data.height));
    camera.x = (viewport.clientWidth - data.width * camera.scale) / 2;
    camera.y = (viewport.clientHeight - data.height * camera.scale) / 2;
    apply();
    initialized = true;
  };
  const readable = () => {
    if (!viewport.clientWidth || !viewport.clientHeight) return;
    camera.scale = Math.max(0.85, Math.min(1, (viewport.clientWidth - 24) / data.width, (viewport.clientHeight - 24) / data.height));
    const root = nodes.get(data.root);
    camera.x = 20 - root.x * camera.scale;
    camera.y = viewport.clientHeight / 2 - (root.y + root.height / 2) * camera.scale;
    apply();
    initialized = true;
  };
  const zoom = (factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) => {
    const next = Math.max(0.001, Math.min(3, camera.scale * factor));
    const ratio = next / camera.scale;
    camera.x = x - (x - camera.x) * ratio;
    camera.y = y - (y - camera.y) * ratio;
    camera.scale = next;
    apply();
  };
  const highlight = id => {
    const related = new Set([id]);
    data.edges.forEach(edge => {
      if (edge.source === id || edge.target === id) { related.add(edge.source); related.add(edge.target); }
    });
    nodeElements.forEach((el, nodeId) => {
      el.classList.toggle("is-dimmed", id !== null && !related.has(nodeId));
      el.classList.toggle("is-selected", nodeId === selected);
      el.setAttribute("aria-pressed", String(nodeId === selected));
    });
    edgeElements.forEach(({el, edge}) => {
      const connected = edge.source === id || edge.target === id;
      el.classList.toggle("is-related", connected);
      el.classList.toggle("is-dimmed", id !== null && !connected);
    });
  };
  const clear = () => { selected = null; inspector.hidden = true; highlight(null); };
  const inspect = id => {
    selected = id;
    const node = nodes.get(id);
    inspector.replaceChildren();
    inspector.hidden = false;
    const close = element("button", "btn-close");
    close.type = "button"; close.setAttribute("aria-label", "Close item details");
    close.addEventListener("click", clear);
    const heading = element("div", "aip-section-heading");
    heading.append(element("h3", "h6", node.name), close);
    inspector.append(heading, element("p", "aip-state-badge mb-2", node.state_label), element("p", "mb-2", `${node.kind_label} · ${number(node.required)} needed across this plan`));
    inspector.querySelector('.aip-state-badge').dataset.state = node.state;
    if (node.step) inspector.append(element("p", "mb-2", `${node.progress}% completed · ${node.ongoing}% ongoing`));
    const reminder = document.querySelector(`[data-checklist][data-item-id="${id}"]`);
    if (reminder) inspector.append(reminder.cloneNode(true));
    if (node.stock) {
      inspector.append(element("p", "mb-2", `${number(node.stock)} from stock`));
      (node.stock_locations || []).forEach(allocation => inspector.append(element("p", "small mb-1", `${number(allocation.quantity)} at ${allocation.name}${allocation.transfer ? " · transport to the assigned industry location" : ""}`)));
    }
    if (node.elsewhere_quantity) {
      const badge = element("span", "aip-state-badge mb-1", "Wrong location");
      badge.dataset.state = "elsewhere";
      inspector.append(badge);
      node.stock_elsewhere.forEach(pile => inspector.append(element("p", "small mb-1", `${number(pile.quantity)} at ${pile.name}`)));
    }
    if (node.output) inspector.append(element("p", "mb-2", `${number(node.output)} to make · ${number(node.runs)} runs`));
    if (node.kind === "buy" && node.missing) inspector.append(element("p", "mb-2", `${number(node.missing)} to buy / import`));
    if (node.covered_quantity) inspector.append(element("p", "mb-2", `${number(node.covered_quantity)} covered by purchase`));
    if (node.facility) inspector.append(element("p", "small text-body-secondary", node.facility + (node.system ? ` · ${node.system}` : "")));
    if (node.kind === "manufacturing") inspector.append(element("p", "small", `Blueprint ME ${node.me} / TE ${node.te}`));
    const actions = element("div", "aip-actions mb-3");
    const focus = element("button", "btn btn-secondary btn-sm", "Center item");
    focus.type = "button"; focus.addEventListener("click", () => { camera.scale = 1; center(node); });
    actions.append(focus);
    if (node.step) {
      const link = element("a", "btn btn-primary btn-sm", `Open step ${node.step}`);
      link.href = `#step-${node.step}`;
      actions.append(link);
    }
    inspector.append(actions);
    const inputs = data.edges.filter(edge => edge.source === id);
    if (inputs.length) {
      inspector.append(element("h4", "h6", "Inputs for this item"));
      const list = element("ul", "aip-graph-inputs");
      inputs.forEach(edge => {
        const row = element("li");
        const button = element("button", "btn btn-link btn-sm p-0 text-start", `${nodes.get(edge.target).name} · ${edgeAmounts(edge)}`);
        button.type = "button"; button.addEventListener("click", () => inspect(edge.target));
        row.append(button); list.append(row);
      });
      inspector.append(list);
    }
    const consumers = data.edges.filter(edge => edge.target === id);
    if (consumers.length) {
      inspector.append(element("h4", "h6 mt-3", "Used by"));
      const list = element("ul", "aip-graph-inputs");
      consumers.forEach(edge => {
        const row = element("li");
        const button = element("button", "btn btn-link btn-sm p-0 text-start", `${nodes.get(edge.source).name} · ${edgeAmounts(edge)}`);
        button.type = "button"; button.addEventListener("click", () => inspect(edge.source));
        row.append(button); list.append(row);
      });
      inspector.append(list);
    }
    highlight(id);
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", data.width); svg.setAttribute("height", data.height);
  svg.setAttribute("aria-hidden", "true"); svg.classList.add("aip-graph-edges");
  data.edges.forEach(edge => {
    const source = nodes.get(edge.source), target = nodes.get(edge.target);
    const sx = source.x + source.width, sy = source.y + source.height / 2;
    const tx = target.x, ty = target.y + target.height / 2, mid = (sx + tx) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ty}, ${tx} ${ty}`);
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(path); edgeElements.push({el: path, edge});
    path.dataset.state = edge.quantity ? target.state : "covered";
    if (target.shared) path.dataset.shared = "true";
  });
  world.style.width = `${data.width}px`; world.style.height = `${data.height}px`;
  world.append(svg);
  data.nodes.forEach(node => {
    const button = element("button", `aip-graph-node text-start${node.checked ? " is-reviewed" : ""}`);
    button.type = "button"; button.dataset.graphNode = node.id;
    button.dataset.state = node.state; button.dataset.shared = String(node.shared); button.dataset.reviewItem = node.id;
    button.style.left = `${node.x}px`; button.style.top = `${node.y}px`;
    button.style.width = `${node.width}px`; button.style.height = `${node.height}px`;
    button.title = `${node.name}\n${node.kind_label} · ${number(node.required)} needed`;
    button.setAttribute("aria-label", `${node.name}. ${node.kind_label}. ${node.state_label}. ${number(node.required)} needed.${node.step ? ` Step ${node.step}, ${node.progress}% completed, ${node.ongoing}% ongoing.` : ""}${node.shared ? " Shared input." : ""}`);
    const heading = element("span", "aip-graph-node-heading");
    heading.append(icon(node.id), element("strong", "aip-graph-node-name", node.name));
    button.append(heading, element("strong", "aip-graph-node-quantity", `Need ${number(node.required)}`));
    const caption = node.state === "covered" ? `${number(node.covered_quantity)} covered` : node.output ? `Make ${number(node.output)}${node.stock ? ` · Stock ${number(node.stock)}` : ""}` : node.kind === "buy" ? `Buy ${number(node.missing)}${node.stock ? ` · Stock ${number(node.stock)}` : ""}` : `Stock ${number(node.stock)}`;
    button.append(element("span", "aip-graph-node-caption", caption));
    button.append(element("span", "aip-graph-node-status", node.state_label));
    const footer = element("span", "aip-graph-node-footer");
    const kind = node.elsewhere_quantity && node.state !== "elsewhere" ? element("span", "aip-graph-kind aip-elsewhere-label", "Wrong location") : element("span", "aip-graph-kind", node.kind_label);
    footer.append(kind, element("span", "", node.step ? `✓ ${node.progress}% · ▶ ${node.ongoing}%` : node.shared ? "Shared input" : ""));
    button.append(footer);
    button.addEventListener("click", () => inspect(node.id));
    button.addEventListener("focus", () => {
      const left = camera.x + node.x * camera.scale;
      const top = camera.y + node.y * camera.scale;
      if (left < 0 || top < 0 || left + node.width * camera.scale > viewport.clientWidth || top + node.height * camera.scale > viewport.clientHeight) center(node);
      highlight(node.id);
    });
    button.addEventListener("blur", () => highlight(selected));
    button.addEventListener("mouseenter", () => highlight(node.id));
    button.addEventListener("mouseleave", () => highlight(selected));
    world.append(button); nodeElements.set(node.id, button);
  });
  panel.querySelectorAll("[data-graph-zoom]").forEach(button => button.addEventListener("click", () => zoom(button.dataset.graphZoom === "in" ? 1.25 : 0.8)));
  panel.querySelector("[data-graph-fit]").addEventListener("click", fit);
  panel.querySelector("[data-graph-reset]").addEventListener("click", readable);
  viewport.addEventListener("wheel", event => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - rect.left, event.clientY - rect.top);
  }, {passive: false});
  let drag = null, moved = false;
  viewport.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("[data-graph-node]")) return;
    moved = false;
    drag = {x: event.clientX, y: event.clientY, cameraX: camera.x, cameraY: camera.y};
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("is-dragging");
  });
  viewport.addEventListener("pointermove", event => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    camera.x = drag.cameraX + dx; camera.y = drag.cameraY + dy;
    apply();
  });
  const endDrag = () => { drag = null; viewport.classList.remove("is-dragging"); };
  viewport.addEventListener("pointerup", endDrag); viewport.addEventListener("pointercancel", endDrag);
  viewport.addEventListener("click", event => { if (!moved && !event.target.closest("[data-graph-node]")) clear(); });
  viewport.addEventListener("keydown", event => {
    if (event.key === "Escape") { clear(); return; }
    if (event.key === "+" || event.key === "=") zoom(1.25);
    else if (event.key === "-") zoom(0.8);
    else if (event.key === "0") fit();
    else if (event.key === "ArrowLeft") { camera.x += 70; apply(); }
    else if (event.key === "ArrowRight") { camera.x -= 70; apply(); }
    else if (event.key === "ArrowUp") { camera.y += 70; apply(); }
    else if (event.key === "ArrowDown") { camera.y -= 70; apply(); }
    else return;
    event.preventDefault();
  });
  const fullscreen = panel.querySelector("[data-graph-fullscreen]");
  if (!graphPane.requestFullscreen) fullscreen.hidden = true;
  else fullscreen.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement === graphPane) await document.exitFullscreen();
      else await graphPane.requestFullscreen();
    } catch (_) { fullscreen.textContent = "Fullscreen unavailable"; }
  });
  document.addEventListener("fullscreenchange", () => { fullscreen.textContent = document.fullscreenElement === graphPane ? "Exit fullscreen" : "Fullscreen"; });
  const setView = view => {
    graphPane.hidden = view !== "graph"; treePane.hidden = view !== "tree";
    panel.querySelectorAll("[data-plan-view]").forEach(button => {
      const active = button.dataset.planView === view;
      button.classList.toggle("btn-primary", active); button.classList.toggle("btn-outline-primary", !active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (view === "graph" && !initialized) requestAnimationFrame(data.roots?.length > 1 ? fit : readable);
  };
  panel.querySelectorAll("[data-plan-view]").forEach(button => button.addEventListener("click", () => {
    setView(button.dataset.planView);
    try { sessionStorage.setItem("aip-build-view", button.dataset.planView); } catch (_) { /* Optional preference. */ }
  }));
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => {
    if (!graphPane.hidden) { if (!initialized) readable(); else apply(); }
  }).observe(viewport);
  let initial = "graph";
  try { if (sessionStorage.getItem("aip-build-view") === "tree") initial = "tree"; } catch (_) { /* Optional preference. */ }
  setView(initial);
})();
