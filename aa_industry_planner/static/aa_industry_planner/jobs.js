(() => {
  "use strict";

  const notice = document.getElementById("job-notification-preview");
  if (notice) {
    const buttons = document.querySelectorAll("[data-delivery-preview]");
    const fields = notice.querySelectorAll("[data-notice-field]");
    for (const button of buttons) {
      button.addEventListener("click", () => {
        for (const field of fields) field.textContent = button.dataset[field.dataset.noticeField];
        for (const choice of buttons) choice.setAttribute("aria-pressed", String(choice === button));
        notice.scrollIntoView({block: "nearest"});
      });
    }
  }

  const chart = document.querySelector("[data-job-burndown]");
  if (!chart) return;

  const data = JSON.parse(document.getElementById("job-burndown-data").textContent);
  const target = chart.querySelector("[data-burndown-target]");
  const svg = chart.querySelector("[data-burndown-svg]");
  const tooltip = chart.querySelector("[data-burndown-tooltip]");
  const timestamp = tooltip.querySelector("time");
  const status = chart.querySelector("[data-burndown-status]");
  const buttons = chart.querySelectorAll("[data-burndown-window]");
  const series = ["reaction", "manufacturing"].map(activity => ({
    activity,
    finishes: data[activity].sort((a, b) => a - b),
    label: tooltip.querySelector(`[data-slot-count="${activity}"]`),
  }));
  const finishes = [...new Set(series.flatMap(item => item.finishes))].sort((a, b) => a - b);
  const minute = 60000;
  const hour = 60 * minute;
  const day = 24 * hour;
  const windows = {day, week: 7 * day, max: finishes.at(-1) - data.now};
  let windowChoice = "day";
  let selectedTime;
  let plot;

  function shape(tag, attributes, label) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    if (label !== undefined) element.textContent = label;
    return element;
  }

  function offsetLabel(offset) {
    if (offset === 0) return "Now";
    if (offset === day) return "+24h";
    if (offset < minute) return `+${Math.max(1, Math.floor(offset / 1000))}s`;
    const parts = [
      [Math.floor(offset / day), "d"],
      [Math.floor(offset / hour) % 24, "h"],
      [Math.floor(offset / minute) % 60, "m"],
    ];
    return "+" + parts.filter(([value]) => value).map(([value, unit]) => `${value}${unit}`).join(" ");
  }

  function clearReadout() {
    selectedTime = undefined;
    tooltip.hidden = true;
    status.textContent = "";
    plot?.cursor.setAttribute("visibility", "hidden");
  }

  function draw() {
    const {width, height} = svg.getBoundingClientRect();
    clearReadout();
    const left = 34, right = width - 12, top = 24, bottom = height - 26;
    if (right <= left || bottom <= top) {
      svg.replaceChildren();
      plot = undefined;
      return;
    }
    const horizon = windows[windowChoice];
    const end = data.now + horizon;
    const peak = Math.max(...series.map(item => item.finishes.length));
    const step = Math.max(1, Math.ceil(peak / 4));
    const ceiling = Math.ceil(peak / step) * step;
    const x = time => left + (time - data.now) / horizon * (right - left);
    const y = slots => bottom - slots / ceiling * (bottom - top);
    const marks = [shape("text", {x: left, y: 13}, "Slots used")];
    for (let slots = 0; slots <= ceiling; slots += step) {
      const at = y(slots);
      marks.push(shape("line", {x1: left, x2: right, y1: at, y2: at, class: "aip-burndown-grid"}));
      marks.push(shape("text", {x: left - 6, y: at + 4, "text-anchor": "end"}, slots));
    }
    const offsets = windowChoice === "week" ? [0, 2 * day, 4 * day, 6 * day, horizon]
      : width < 420 ? [0, horizon / 2, horizon] : [0, horizon / 4, horizon / 2, horizon * .75, horizon];
    const endLabel = offsetLabel(horizon);
    const seenLabels = new Set([endLabel]);
    for (const offset of offsets) {
      const at = x(data.now + offset);
      const label = offsetLabel(offset);
      if (offset !== horizon && (seenLabels.has(label) || right - at < 52)) continue;
      seenLabels.add(label);
      const anchor = offset === 0 ? "start" : offset === horizon ? "end" : "middle";
      marks.push(shape("line", {x1: at, x2: at, y1: top, y2: bottom, class: "aip-burndown-grid"}));
      marks.push(shape("text", {x: at, y: bottom + 17, "text-anchor": anchor}, label));
    }
    for (const {activity, finishes: events} of series) {
      const path = [`M ${left} ${y(events.length)}`];
      for (let i = 0; i < events.length && events[i] <= end; i++) {
        path.push(`H ${x(events[i])} V ${y(events.length - i - 1)}`);
      }
      path.push(`H ${right}`);
      marks.push(shape("path", {d: path.join(" "), class: `aip-job-${activity}`, stroke: "currentColor", fill: "none"}));
    }
    const cursor = shape("g", {class: "aip-burndown-cursor", visibility: "hidden"});
    const stops = [data.now, ...finishes.filter(time => time < end), end];
    plot = {width, left, right, top, bottom, end, x, y, stops, cursor};
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.replaceChildren(...marks, cursor);
  }

  function inspect(time, announce = false) {
    if (time === selectedTime && !announce) return;
    selectedTime = time;
    const [reactions, manufacturing] = series.map(({finishes: events}) => events.filter(end => end > time).length);
    series[0].label.textContent = `${reactions} ${reactions === 1 ? "reaction" : "reactions"}`;
    series[1].label.textContent = `${manufacturing} manufacturing`;
    const date = new Date(time);
    const twoDigits = number => String(number).padStart(2, "0");
    timestamp.textContent = `${twoDigits(date.getHours())}:${twoDigits(date.getMinutes())} ${twoDigits(date.getDate())}/${twoDigits(date.getMonth() + 1)}`;
    timestamp.dateTime = date.toISOString();
    const {width, x, y, top, bottom, cursor} = plot;
    const at = x(time);
    cursor.replaceChildren(
      shape("line", {x1: at, x2: at, y1: top, y2: bottom}),
      shape("rect", {x: at - 5, y: y(manufacturing) - 5, width: 10, height: 10, rx: 1, class: "aip-job-manufacturing", stroke: "currentColor"}),
      shape("circle", {cx: at, cy: y(reactions), r: 4, class: "aip-job-reaction", fill: "currentColor"}),
    );
    cursor.setAttribute("visibility", "visible");
    tooltip.hidden = false;
    const tipWidth = tooltip.offsetWidth;
    const tipLeft = at + tipWidth + 12 <= width ? at + 12 : at - tipWidth - 12;
    tooltip.style.left = `${Math.max(0, tipLeft)}px`;
    if (announce) status.textContent = `${series[0].label.textContent}, ${series[1].label.textContent}, ${timestamp.textContent}`;
  }

  function inspectPointer(event) {
    if (!plot) return;
    const bounds = svg.getBoundingClientRect();
    const px = event.clientX - bounds.left, py = event.clientY - bounds.top;
    const {left, right, top, bottom, end, x, stops} = plot;
    if (px < left - 5 || px > right + 5 || py < top - 5 || py > bottom + 5) {
      clearReadout();
      return;
    }
    const fraction = Math.max(0, Math.min(1, (px - left) / (right - left)));
    let time = Math.round(data.now + fraction * (end - data.now));
    let distance = 4;
    for (const stop of stops) {
      const gap = Math.abs(x(stop) - px);
      if (gap < distance) {
        time = stop;
        distance = gap;
      }
    }
    inspect(time);
  }

  for (const button of buttons) {
    button.addEventListener("click", () => {
      windowChoice = button.dataset.burndownWindow;
      for (const choice of buttons) {
        choice.classList.toggle("active", choice === button);
        choice.setAttribute("aria-pressed", String(choice === button));
      }
      draw();
    });
  }
  target.addEventListener("pointerdown", inspectPointer);
  target.addEventListener("pointermove", event => {
    if (event.pointerType !== "touch") inspectPointer(event);
  });
  target.addEventListener("pointerleave", event => {
    if (event.pointerType !== "touch") clearReadout();
  });
  target.addEventListener("focus", () => {
    if (plot && target.matches(":focus-visible")) inspect(data.now, true);
  });
  target.addEventListener("blur", clearReadout);
  target.addEventListener("keydown", event => {
    if (!plot) return;
    const {stops, end} = plot;
    const time = selectedTime ?? data.now;
    let nextTime;
    switch (event.key) {
      case "ArrowLeft": nextTime = stops.findLast(stop => stop < time) ?? data.now; break;
      case "ArrowRight": nextTime = stops.find(stop => stop > time) ?? end; break;
      case "Home": nextTime = data.now; break;
      case "End": nextTime = end; break;
      case "Escape": clearReadout(); return;
      default: return;
    }
    event.preventDefault();
    inspect(nextTime, true);
  });
  draw();
  new ResizeObserver(draw).observe(svg);
})();
