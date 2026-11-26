const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');
const source = readFileSync(resolve(__dirname, '../aa_industry_planner/static/aa_industry_planner/index_history.js'), 'utf8');

class Element {
  constructor() { this.children = []; this.style = {}; this.dataset = {}; this.attributes = {}; this.disabled = true; }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, callback) { this[event] = callback; }
}
async function browser(samples, {ok = true} = {}) {
  const legend = new Element(), status = new Element(), reactionButton = new Element(), allButton = new Element();
  const canvas = new Element(), frames = [], requests = [];
  let frame, path;
  const ctx = {
    scale() { frame = {strokes: [], dots: [], labels: []}; frames.push(frame); },
    beginPath() { path = []; },
    moveTo(x, y) { path.push({kind: 'move', x, y}); },
    lineTo(x, y) { path.push({kind: 'line', x, y}); },
    arc(x, y, radius) { frame.dots.push({x, y, radius}); },
    fill() {}, setLineDash(dash) { this.dash = [...dash]; },
    stroke() { frame.strokes.push({colour: this.strokeStyle, width: this.lineWidth, dash: this.dash, path}); },
    fillText(text) { frame.labels.push(text); },
  };
  canvas.getContext = () => ctx;
  const chart = {
    clientWidth: 1000, dataset: {indexHistory: '/industry/systems/30000142/history/'},
    querySelector: selector => ({canvas, '.aip-chart-status': status, '.aip-chart-legend': legend,
      '[data-reactions-only]': reactionButton, '[data-show-all-indices]': allButton})[selector],
  };
  runInNewContext(source, {
    document: {querySelectorAll: () => [chart], documentElement: {}, createElement: () => new Element(), createTextNode: text => ({textContent: text})},
    window: {devicePixelRatio: 2},
    getComputedStyle: () => ({getPropertyValue: key => key === '--bs-body-bg' ? '#222' : '#ddd'}),
    ResizeObserver: class {observe() {}}, MutationObserver: class {observe() {}},
    fetch: async (url, options) => { requests.push({url, options}); return {ok, json: async () => ({samples})}; },
  });
  await new Promise(setImmediate);
  return {legend, status, reactionButton, allButton, frames, requests,
    get frame() { return frames.at(-1); },
    key(activity) { return legend.children.find(child => child.dataset.activity === activity); },
  };
}
const sample = (activity, cost_index, day = 1) => ({activity, cost_index, recorded_at: `2026-11-${String(day).padStart(2, '0')}T12:00:00Z`});
const reactionLines = ui => ui.frame.strokes.filter(stroke => stroke.colour === '#d24dad' && stroke.path.some(point => point.kind === 'line'));

test('reactions retain a thick magenta line above overlapping activities regardless of response order', async () => {
  const ui = await browser([
    sample('researching_time_efficiency', '.0042', 2), sample('researching_material_efficiency', '.0042', 2),
    sample('reaction', '.0042', 2), sample('manufacturing', '.0042', 2),
    sample('researching_time_efficiency', '.0042', 1), sample('researching_material_efficiency', '.0042', 1),
    sample('reaction', '.0042', 1), sample('manufacturing', '.0042', 1),
  ]);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].options.credentials, 'same-origin');
  const lines = ui.frame.strokes.filter(stroke => stroke.path.some(point => point.kind === 'line'));
  assert.equal(lines.at(-1).colour, '#d24dad');
  assert.equal(lines.at(-1).width, 3.5);
  assert.deepEqual(lines.at(-1).dash, []);
  assert.ok(lines.at(-1).path[0].x < lines.at(-1).path[1].x);
  assert.equal(ui.frame.dots.length, 2);
  assert.equal(ui.key('reaction').attributes['aria-pressed'], 'true');
  assert.deepEqual(ui.legend.children.map(key => key.children[1].textContent), [
    'Manufacturing', 'ME research', 'TE research', 'Reactions',
  ]);
});

test('isolating reactions rescales the chart and show all restores every activity', async () => {
  const ui = await browser([sample('reaction', '.0042'), sample('reaction', '.0041', 2), sample('invention', '.1')]);
  const axisMaximum = () => Math.max(...ui.frame.labels.filter(label => label.endsWith('%')).map(label => parseFloat(label)));
  assert.ok(axisMaximum() >= 10);
  ui.reactionButton.click();
  assert.ok(axisMaximum() < 1);
  assert.equal(ui.key('invention').attributes['aria-pressed'], 'false');
  assert.equal(ui.reactionButton.attributes['aria-pressed'], 'true');
  assert.match(ui.status.textContent, /^Reactions ·/);
  assert.equal(reactionLines(ui).length, 1);
  ui.allButton.click();
  assert.ok(axisMaximum() >= 10);
  assert.equal(ui.key('invention').attributes['aria-pressed'], 'true');
  assert.equal(ui.reactionButton.attributes['aria-pressed'], 'false');
});

test('legend toggles allow an empty selection and the chart recovers', async () => {
  const ui = await browser([sample('reaction', '.0014')]);
  assert.equal(ui.frame.dots.length, 1);
  ui.key('reaction').click();
  assert.equal(ui.frame.dots.length, 0);
  assert.match(ui.status.textContent, /Choose an activity/);
  ui.allButton.click();
  assert.equal(ui.frame.dots.length, 1);
  assert.equal(ui.key('reaction').attributes['aria-pressed'], 'true');
});

test('missing or invalid reaction data disables isolation without hiding other activities', async () => {
  const ui = await browser([sample('manufacturing', '.003'), sample('reaction', null), sample('reaction', 'invalid')]);
  assert.equal(ui.reactionButton.disabled, true);
  assert.equal(ui.allButton.disabled, false);
  assert.equal(ui.key('reaction'), undefined);
  assert.match(ui.status.textContent, /^Manufacturing ·/);
});

test('empty history and failed requests explain why no chart is displayed', async () => {
  const empty = await browser([]);
  assert.match(empty.status.textContent, /No history collected/);
  assert.equal(empty.allButton.disabled, true);
  const failed = await browser([], {ok: false});
  assert.match(failed.status.textContent, /Unable to load index history/);
  assert.equal(failed.reactionButton.disabled, true);
});
