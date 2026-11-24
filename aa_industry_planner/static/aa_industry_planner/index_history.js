(() => {
  "use strict";
  // Key colours by activity, rather than the order ESI returns its samples.
  const styles = {
    manufacturing: {label: "Manufacturing", colour: "#3689e0", dash: [8, 4]},
    researching_time_efficiency: {label: "TE research", colour: "#e19b20", dash: [3, 3]},
    researching_material_efficiency: {label: "ME research", colour: "#4bab78", dash: [10, 3, 2, 3]},
    copying: {label: "Copying", colour: "#9e84e8", dash: [6, 3]},
    invention: {label: "Invention", colour: "#4aafb9", dash: [2, 4]},
    // Draw reactions last so an identical series cannot cover the reaction line.
    reaction: {label: "Reactions", colour: "#d24dad", dash: []},
  };
  document.querySelectorAll(".aip [data-index-history]").forEach(async chart => {
    const canvas = chart.querySelector("canvas");
    const status = chart.querySelector(".aip-chart-status");
    const legend = chart.querySelector(".aip-chart-legend");
    const reactionsOnly = chart.querySelector("[data-reactions-only]");
    const showAll = chart.querySelector("[data-show-all-indices]");
    try {
      const response = await fetch(chart.dataset.indexHistory, {credentials: "same-origin"});
      if (!response.ok) throw new Error("History unavailable");
      const {samples} = await response.json();
      const grouped = new Map();
      for (const sample of samples) {
        const x = new Date(sample.recorded_at).getTime(), y = Number(sample.cost_index) * 100;
        if (!Number.isFinite(x) || !Number.isFinite(y) || y < 0 || sample.cost_index == null) continue;
        if (!grouped.has(sample.activity)) grouped.set(sample.activity, []);
        grouped.get(sample.activity).push({x, y});
      }
      const order = Object.keys(styles);
      const series = [...grouped].map(([activity, points]) => ({
        activity, points: points.sort((a, b) => a.x - b.x),
        style: styles[activity] || {label: activity.replaceAll("_", " "), colour: "#888888", dash: [4, 4]},
      })).sort((a, b) => {
        // Unknown future activities still render below the highlighted reaction series.
        const rank = activity => activity === "reaction" ? order.length + 1 : order.indexOf(activity);
        return rank(a.activity) - rank(b.activity);
      });
      if (!series.length) { status.textContent = "No history collected yet."; return; }
      const visible = new Set(series.map(item => item.activity));
      const buttons = new Map();
      const times = series.flatMap(item => item.points.map(point => point.x));
      const first = Math.min(...times), last = Math.max(...times);
      const end = Math.max(first + 3600000, last);
      const format = new Intl.NumberFormat(undefined, {maximumFractionDigits: 4});
      const date = value => new Date(value).toLocaleDateString(undefined, {day: "numeric", month: "short"});
      const draw = () => {
        const width = Math.max(280, chart.clientWidth), height = 260;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = width * ratio; canvas.height = height * ratio;
        const ctx = canvas.getContext("2d");
        ctx.scale(ratio, ratio);
        const css = getComputedStyle(chart);
        const text = css.getPropertyValue("--bs-body-color").trim() || css.color;
        const grid = css.getPropertyValue("--bs-border-color").trim() || "#777";
        const background = css.getPropertyValue("--bs-body-bg").trim() || "#fff";
        const selected = series.filter(item => visible.has(item.activity));
        const peak = Math.max(.01, ...selected.flatMap(item => item.points.map(point => point.y)));
        const magnitude = 10 ** Math.floor(Math.log10(peak));
        const max = Math.ceil(peak * 1.1 / magnitude) * magnitude;
        const left = 72, right = width - 14, top = 14, bottom = height - 30;
        const x = value => left + (value - first) / (end - first) * (right - left);
        const y = value => bottom - value / max * (bottom - top);
        ctx.font = "12px sans-serif";
        ctx.lineWidth = 1; ctx.setLineDash([]);
        for (let tick = 0; tick <= 4; tick++) {
          const value = max * tick / 4, position = y(value);
          ctx.strokeStyle = grid;
          ctx.beginPath(); ctx.moveTo(left, position); ctx.lineTo(right, position); ctx.stroke();
          ctx.fillStyle = text; ctx.textAlign = "right";
          ctx.fillText(`${format.format(value)}%`, left - 10, position + 4);
        }
        ctx.fillStyle = text;
        ctx.textAlign = "left"; ctx.fillText(date(first), left, height - 6);
        ctx.textAlign = "right"; ctx.fillText(date(end), right, height - 6);
        for (const item of selected) {
          const reaction = item.activity === "reaction";
          const path = () => {
            ctx.beginPath();
            item.points.forEach((point, index) => {
              if (index) ctx.lineTo(x(point.x), y(point.y)); else ctx.moveTo(x(point.x), y(point.y));
            });
            ctx.stroke();
          };
          ctx.setLineDash(item.style.dash);
          if (reaction) {
            ctx.strokeStyle = background; ctx.lineWidth = 6; path();
          }
          ctx.strokeStyle = item.style.colour; ctx.lineWidth = reaction ? 3.5 : 2;
          path();
          // Mark sparse reaction samples too: a single sample must still be visible.
          const stride = Math.max(1, Math.ceil(item.points.length / 14));
          item.points.forEach((point, index) => {
            if (item.points.length > 1 && (!reaction || (index % stride && index !== item.points.length - 1))) return;
            ctx.beginPath(); ctx.arc(x(point.x), y(point.y), reaction ? 3.5 : 3, 0, 2 * Math.PI);
            ctx.fillStyle = item.style.colour; ctx.fill();
            if (reaction) { ctx.setLineDash([]); ctx.strokeStyle = background; ctx.lineWidth = 1; ctx.stroke(); }
          });
        }
        buttons.forEach((button, activity) => button.setAttribute("aria-pressed", String(visible.has(activity))));
        const onlyReactions = visible.size === 1 && visible.has("reaction");
        reactionsOnly.setAttribute("aria-pressed", String(onlyReactions));
        status.textContent = selected.length
          ? `${selected.map(item => item.style.label).join(" · ")} · ${date(first)}–${date(last)} · hourly samples, up to 90 days. Scale follows visible activities.`
          : "Choose an activity in the legend, or show all activities.";
        canvas.setAttribute("aria-label", selected.length
          ? `System cost index history. ${status.textContent}` : "No activities selected.");
      };
      for (const item of series) {
        const button = document.createElement("button"), swatch = document.createElement("span");
        button.type = "button"; button.className = "aip-chart-key";
        button.dataset.activity = item.activity;
        button.setAttribute("aria-pressed", "true");
        swatch.className = "aip-chart-swatch"; swatch.style.borderColor = item.style.colour;
        swatch.style.borderTopStyle = item.style.dash.length ? "dashed" : "solid";
        swatch.setAttribute("aria-hidden", "true");
        button.append(swatch, document.createTextNode(item.style.label));
        button.addEventListener("click", () => {
          if (visible.has(item.activity)) visible.delete(item.activity); else visible.add(item.activity);
          draw();
        });
        buttons.set(item.activity, button); legend.append(button);
      }
      reactionsOnly.disabled = !grouped.has("reaction"); showAll.disabled = false;
      reactionsOnly.addEventListener("click", () => { visible.clear(); visible.add("reaction"); draw(); });
      showAll.addEventListener("click", () => { series.forEach(item => visible.add(item.activity)); draw(); });
      draw();
      new ResizeObserver(draw).observe(chart);
      new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ["data-bs-theme", "class", "style"]});
    } catch {
      status.textContent = "Unable to load index history. Reload the page to retry.";
    }
  });
})();
