const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const {runInNewContext} = require("node:vm");
const source = readFileSync(resolve(__dirname, "../aa_industry_planner/static/aa_industry_planner/industry.js"), "utf8");

class UIEvent {
  constructor(type, options = {}) { Object.assign(this, options); this.type = type; this.defaultPrevented = false; }
  preventDefault() { this.defaultPrevented = true; }
}
class Element {
  constructor() {
    this.children = []; this.attributes = {}; this.events = {}; this.style = {}; this.value = "";
    const classes = new Set();
    this.classList = {
      add: token => classes.add(token), remove: token => classes.delete(token), contains: token => classes.has(token),
      toggle: (token, force) => force ? classes.add(token) : classes.delete(token),
    };
  }
  append(...children) { for (const child of children) { this.children.push(child); child.parent = this; } }
  replaceWith(element) { const parent = this.parent; parent.children[parent.children.indexOf(this)] = element; element.parent = parent; }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, callback) { (this.events[type] ??= []).push(callback); }
  dispatchEvent(event) { for (const callback of this.events[event.type] || []) callback(event); }
  scrollIntoView() {}
}
function browser(fetchResponse = async () => ({ok: true, json: async () => ({results: [{id: 35825, name: "Raitaru", value: "35825 · Raitaru"}]})})) {
  const root = new Element(), input = new Element(), requests = [], timers = new Map();
  root.append(input);
  let timerId = 0;
  const app = {
    dataset: {typeSearchUrl: "/industry/types/"},
    querySelectorAll: selector => selector === ".aip-nav a" ? [] : selector === "[data-type-search]" ? [input] : [],
  };
  // Navigation needs its overview URL, but no links are rendered by this fixture.
  const nav = {href: "https://example.test/industry/", classList: {toggle() {}}, setAttribute() {}, removeAttribute() {}};
  app.querySelectorAll = selector => selector === ".aip-nav a" ? [nav] : selector === "[data-type-search]" ? [input] : [];
  runInNewContext(source, {
    document: {querySelector: () => app, createElement: () => new Element()},
    location: {pathname: "/industry/plans/"}, URL, Event: UIEvent, AbortController, Date,
    setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => { requests.push({url, options}); return fetchResponse(url, options); },
  });
  const menu = () => input.parent.children.find(child => child.attributes.role === "listbox");
  return {
    input, requests, menu,
    type(value) { input.value = value; input.dispatchEvent(new UIEvent("input")); },
    key(key) { const event = new UIEvent("keydown", {key}); input.dispatchEvent(event); return event; },
    fireTimers() { const callbacks = [...timers.values()]; timers.clear(); return callbacks.map(callback => callback()); },
    async settle() { await Promise.all(this.fireTimers()); },
  };
}

test("suggestions appear on response and mouse selection retains the item ID", async () => {
  const ui = browser();
  ui.type("Rai");
  await ui.settle();
  assert.equal(ui.menu().classList.contains("show"), true);
  assert.equal(ui.menu().children[0].textContent, "Raitaru");
  ui.menu().children[0].dispatchEvent(new UIEvent("click"));
  assert.equal(ui.input.value, "35825 · Raitaru");
  assert.equal(ui.menu().classList.contains("show"), false);
});

test("keyboard selection works and a repeated search avoids another request", async () => {
  const ui = browser();
  ui.type("Rai");
  await ui.settle();
  assert.equal(ui.key("ArrowDown").defaultPrevented, true);
  assert.equal(ui.input.attributes["aria-activedescendant"], "industry-type-options-0-0");
  assert.equal(ui.key("Enter").defaultPrevented, true);
  assert.equal(ui.input.value, "35825 · Raitaru");
  ui.type("Rai");
  assert.equal(ui.menu().children[0].textContent, "Raitaru");
  await ui.settle();
  assert.equal(ui.requests.length, 1);
});

test("an older response cannot replace the current query's suggestions", async () => {
  const responses = new Map();
  const ui = browser(url => new Promise(resolve => responses.set(url.split("q=")[1], resolve)));
  ui.type("Rai");
  const [oldRequest] = ui.fireTimers();
  ui.type("Tat");
  const [newRequest] = ui.fireTimers();
  responses.get("Tat")({ok: true, json: async () => ({results: [{name: "Tatara", value: "35836 · Tatara"}]})});
  await newRequest;
  responses.get("Rai")({ok: true, json: async () => ({results: [{name: "Raitaru", value: "35825 · Raitaru"}]})});
  await oldRequest;
  assert.equal(ui.menu().children[0].textContent, "Tatara");
});

test("leaving the field prevents a late response from reopening the menu", async () => {
  let respond;
  const ui = browser(() => new Promise(resolve => { respond = resolve; }));
  ui.type("Rai");
  const [pending] = ui.fireTimers();
  ui.input.dispatchEvent(new UIEvent("blur"));
  respond({ok: true, json: async () => ({results: [{name: "Raitaru", value: "35825 · Raitaru"}]})});
  await pending;
  assert.equal(ui.menu().classList.contains("show"), false);
});

test("typing quickly sends only the final query and Escape closes suggestions", async () => {
  const ui = browser();
  for (const query of ["R", "Ra", "Rai"]) ui.type(query);
  await ui.settle();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, "/industry/types/?q=Rai");
  ui.key("Escape");
  assert.equal(ui.input.attributes["aria-expanded"], "false");
});

test("failed searches show an error and remain retryable", async () => {
  const ui = browser(async () => ({ok: false}));
  ui.type("Rai");
  await ui.settle();
  assert.match(ui.menu().children[0].textContent, /Search unavailable/);
  assert.equal(ui.input.attributes["aria-busy"], undefined);
  ui.type("Rai");
  await ui.settle();
  assert.equal(ui.requests.length, 2);
});
