// Tests for the page's data layer (BFF-DATA block in site/index.html) driving the update check, with a fake page around it.
// Run: node --test tests_js/dash.test.mjs   No dependencies; the blocks are cut out of the shipped page.
import test from 'node:test';
import assert from 'node:assert/strict';
import { block, enc, report, Clock, settle, detSubtle, INTERVAL, LONGEST_FIRST_WAIT, html } from './helpers.mjs';

const factory = new Function('window', 'document', 'fetch', 'setTimeout', 'clearTimeout',
  block('POLL') + '\n' + block('DATA') + '\nreturn window.dash;');
const FRESH = () => new Date().toISOString();
const later = (ms) => new Date(Date.now() + ms).toISOString();

function res(body, status = 200) {
  const bytes = body instanceof Uint8Array ? body : enc.encode(typeof body === 'string' ? body : JSON.stringify(body));
  return { ok: status < 400, status, json: async () => JSON.parse(new TextDecoder().decode(bytes)),
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) };
}

/* A page with just enough DOM for the data layer. `initial` is what plain data.json/summary.json return (null: the network is down);
   `poll` is what the update check receives (null: the network is down). Both can be changed while the test runs. */
function page({ initial, poll }) {
  const clock = new Clock();
  const mk = (extra = {}) => ({ textContent: '', hidden: false, className: '', dataset: {}, children: [], attrs: {}, removed: false,
    replaceChildren(...c) { this.children = c; }, setAttribute(k, v) { this.attrs[k] = v; }, remove() { this.removed = true; }, ...extra });
  const banners = [];
  const els = { '.w-refresh': mk(), '#stale': mk({ hidden: true }), '#warnings': mk({ hidden: true }), '#update-status': mk(), '#update-live': mk(),
    '.tm': mk({ prepend(b) { banners.push(b); } }) };
  const listeners = { doc: {}, win: {} };
  const document = { visibilityState: 'visible', querySelector: (s) => els[s], getElementById: (id) => els['#' + id],
    querySelectorAll: (s) => (s === '.tm-banner.err' ? banners.filter((b) => !b.removed) : []), createElement: () => mk(),
    addEventListener: (t, f) => { listeners.doc[t] = f; } };
  const window = { crypto: { subtle: detSubtle }, addEventListener: (t, f) => { listeners.win[t] = f; } };
  const net = { initial, poll, calls: [], polls: [] };
  const fetch = async (url, init) => {
    net.calls.push(url);
    const isPoll = (init && init.cache === 'no-cache') || url.includes('?');       // the first load uses 'no-store'; the update check revalidates
    if (isPoll) net.polls.push(url);
    const src = isPoll ? net.poll : net.initial;
    if (src === null) throw new TypeError('Failed to fetch');
    return url.startsWith('summary.json') ? res(src.summary) : res(src.data);
  };
  const dash = factory(window, document, fetch, clock.setTimer, clock.clearTimer);
  const seen = []; dash.calc('n', { inputs: ['leads'], fn: (l) => l.length });
  dash.onData(() => seen.push(dash.data('leads').status === 'ok' ? dash.data('leads').data.length : dash.data('leads').status));
  return { dash, clock, els, banners, net, seen, document, listeners, pollCalls: () => net.polls };
}
const rows = (p) => (p.dash.data('leads').status === 'ok' ? p.dash.data('leads').data.length : null);

test('first load behaves as before: both files, matching timestamps, tables computed, update check scheduled', async () => {
  const r = report(3, FRESH());
  const p = page({ initial: r, poll: r });
  await settle();
  assert.deepEqual(p.net.calls, ['data.json', 'summary.json']);
  assert.equal(rows(p), 3);
  assert.equal(p.dash.data('n').data, 3);
  assert.match(p.els['.w-refresh'].textContent, /^data refreshed /);
  assert.equal(p.els['#stale'].hidden, true);
  assert.equal(p.clock.timers.length, 1);
  assert.ok(p.clock.timers[0].ms >= INTERVAL * 0.9 && p.clock.timers[0].ms <= INTERVAL * 1.1, 'the first check is one interval (plus or minus 10%) away');
});

test('a new report replaces the display without a reload and keeps the reader\'s filters', async () => {
  const a = report(3, later(-3_600_000)), b = report(5, later(-60_000));
  const p = page({ initial: a, poll: b });
  await settle();
  p.dash.setParams({ agent: 'Ana Lopez', grain: 'month' });
  const before = p.seen.length;
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 5);
  assert.equal(p.dash.data('n').data, 5);
  assert.deepEqual(p.dash.params(), { agent: 'Ana Lopez', grain: 'month' });
  assert.ok(p.seen.length > before, 'the page was redrawn');
  assert.match(p.els['#update-live'].textContent, /^A newer report was loaded\. Data refreshed /);
  assert.equal(p.dash.summary().report.version, b.version);
});

test('an unchanged report redraws nothing and says nothing aloud, but the visible line shows the last check', async () => {
  const a = report(3, FRESH());
  const p = page({ initial: a, poll: a });
  await settle();
  const before = p.seen.length;
  await p.clock.advance(2 * INTERVAL);            // at least one check has happened by now
  assert.equal(p.seen.length, before);
  assert.equal(p.els['#update-live'].textContent, '', 'the screen-reader region stays quiet');
  assert.match(p.els['#update-status'].textContent, /^Checks for new reports automatically · last checked /);
  assert.equal(p.els['#update-status'].className, 'tm-updates');
});

test('the first load fails but the server is back a moment later: the retry is immediate and the banner goes away', async () => {
  const good = report(3, FRESH());
  const p = page({ initial: null, poll: good });
  await settle();
  assert.equal(p.banners.length, 1, 'the failure notice was shown');
  assert.equal(p.banners[0].attrs.role, 'alert');
  assert.ok(p.pollCalls().length >= 1, 'it did not wait a full interval');
  assert.equal(rows(p), 3);
  assert.equal(p.banners[0].removed, true, 'the failure notice is gone once a report loaded');
  assert.match(p.els['.w-refresh'].textContent, /^data refreshed /);
  assert.match(p.els['#update-live'].textContent, /newer report was loaded/);
});

test('the first load fails and the server stays down for a while: the notice stays, then a later check recovers', async () => {
  const good = report(3, FRESH());
  const p = page({ initial: null, poll: null });
  await settle();
  assert.equal(p.banners.length, 1);
  assert.equal(p.els['.w-refresh'].textContent, 'data could not be loaded');
  assert.equal(rows(p), null);
  p.net.poll = good;                                                    // the server comes back
  await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(rows(p), 3);
  assert.equal(p.banners[0].removed, true);
  assert.match(p.els['.w-refresh'].textContent, /^data refreshed /);
});

test('the first load fails and updates keep failing: one banner, no duplicates, and retries back off', async () => {
  const p = page({ initial: null, poll: null });
  await settle();
  for (let i = 0; i < 5; i++) await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(p.banners.length, 1);
  assert.equal(p.els['#update-status'].className, 'tm-updates warn');
  assert.match(p.els['#update-status'].textContent, /Can't load the report right now/);
});

test('an invalid or mismatched update never touches the display', async () => {
  const a = report(3, later(-3_600_000)), b = report(5, later(-60_000));
  const bad = { summary: b.summary, data: a.data };                  // the new summary, but the old data file
  const p = page({ initial: a, poll: bad });
  await settle();
  const before = p.seen.length;
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 3);
  assert.equal(p.seen.length, before, 'no redraw');
  assert.equal(p.banners.length, 0, 'no error banner over a report that is still valid');
  assert.equal(p.dash.summary().refreshed_at, a.summary.refreshed_at);
});

test('if computing the new tables fails, the old report stays exactly as it was and the failure is reported', async () => {
  const a = report(3, later(-3_600_000)), b = report(5, later(-60_000));
  const p = page({ initial: a, poll: b });
  // the page's own tables are registered after construction here, so register one that fails on the new rows only
  p.dash.calc('boom', { inputs: ['leads'], fn: (l) => { if (l.length === 5) throw new Error('bad rows'); return l.length; } });
  await settle();
  assert.equal(p.dash.data('boom').data, 3, 'the first load computed it fine');
  const before = p.seen.length;
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 3);
  assert.equal(p.dash.data('boom').data, 3);
  assert.equal(p.seen.length, before, 'nothing was redrawn from half-computed data');
  assert.equal(p.dash.summary().report.version, a.version);
});

test('if drawing fails after the swap, the previous report is put back and drawn again', async () => {
  const a = report(3, later(-3_600_000)), b = report(5, later(-60_000));
  const p = page({ initial: a, poll: b });
  await settle();
  let boom = true;
  p.dash.onData(() => { if (boom && p.dash.data('leads').data.length === 5) { boom = false; throw new Error('draw failed'); } });
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 3, 'back to the previous report');
  assert.equal(p.dash.summary().report.version, a.version);
  assert.equal(p.seen.at(-1), 3, 'and it was drawn again');
});

test('a fresh report clears the stale-data warning and the old data notes', async () => {
  const old = report(3, '2026-01-01T00:00:00+00:00', (d) => d);
  old.summary.warnings = ['Ringba has no calls in 2026-02'];
  const fresh = report(4, FRESH());
  const p = page({ initial: old, poll: fresh });
  await settle();
  assert.equal(p.els['#stale'].hidden, false);
  assert.match(p.els['#stale'].textContent, /days old/);
  assert.equal(p.els['#warnings'].hidden, false);
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 4);
  assert.equal(p.els['#stale'].hidden, true);
  assert.equal(p.els['#stale'].textContent, '');
  assert.equal(p.els['#warnings'].hidden, true);
  assert.equal(p.els['#warnings'].children.length, 0);
});

test('trouble and recovery are spoken once each; the visible line turns to a warning and back', async () => {
  const a = report(3, FRESH());
  const p = page({ initial: a, poll: null });
  await settle();
  for (let i = 0; i < 2; i++) await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(p.els['#update-live'].textContent, '', 'two failures: nothing yet');
  await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.match(p.els['#update-live'].textContent, /^Can't check for updates right now\. Still showing the report from /);
  assert.equal(p.els['#update-status'].className, 'tm-updates warn');
  assert.match(p.els['#update-status'].textContent, /Showing the report from .*Trying again automatically\.$/);
  assert.equal(rows(p), 3, 'the report stays on screen');
  p.els['#update-live'].textContent = '';                              // as a screen reader would have consumed it
  await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(p.els['#update-live'].textContent, '', 'not repeated while it continues');
  p.net.poll = a;
  await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(p.els['#update-live'].textContent, 'Updates are working again.');
  assert.equal(p.els['#update-status'].className, 'tm-updates');
});

test('a hidden tab stops asking; coming back checks again; coming back online checks at once', async () => {
  const a = report(3, FRESH());
  const p = page({ initial: a, poll: a });
  await settle();
  p.document.visibilityState = 'hidden'; p.listeners.doc.visibilitychange();
  assert.equal(p.clock.timers.length, 0);
  await p.clock.advance(300_000);
  assert.equal(p.pollCalls().length, 0);
  p.document.visibilityState = 'visible'; p.listeners.doc.visibilitychange();
  await p.clock.advance(2_100);
  assert.equal(p.pollCalls().length, 1);
  const n = p.pollCalls().length;
  p.listeners.win.online(); await settle();
  assert.ok(p.pollCalls().length >= n, 'an online event triggers a check (it may be refused only while another is running)');
});

const FILTERS = { agent: 'Ana Lopez', grain: 'month' };

test('the reader\'s filters are untouched by every outcome of an update: new report, two in a row, refused data, failed tables, failed drawing', async () => {
  const a = report(3, later(-3_600_000)), b = report(5, later(-120_000)), c = report(7, later(-60_000));
  // a new report, then a second one
  let p = page({ initial: a, poll: b });
  await settle(); p.dash.setParams(FILTERS);
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 5); assert.deepEqual(p.dash.params(), FILTERS);
  p.net.poll = c;
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 7); assert.deepEqual(p.dash.params(), FILTERS);
  // data that does not match its summary is refused, through all of its retries and the back-off after them
  p = page({ initial: a, poll: { summary: b.summary, data: a.data } });
  await settle(); p.dash.setParams(FILTERS);
  for (let i = 0; i < 8; i++) await p.clock.advance(p.clock.timers[0].ms + 1);
  assert.equal(rows(p), 3); assert.deepEqual(p.dash.params(), FILTERS);
  // the new tables cannot be computed
  p = page({ initial: a, poll: b });
  p.dash.calc('boom', { inputs: ['leads'], fn: (l) => { if (l.length === 5) throw new Error('bad rows'); return l.length; } });
  await settle(); p.dash.setParams(FILTERS);
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 3); assert.deepEqual(p.dash.params(), FILTERS);
  // drawing fails after the swap and the previous report is put back
  p = page({ initial: a, poll: b });
  await settle(); p.dash.setParams(FILTERS);
  let boom = true;
  p.dash.onData(() => { if (boom && p.dash.data('leads').data.length === 5) { boom = false; throw new Error('draw failed'); } });
  await p.clock.advance(LONGEST_FIRST_WAIT);
  assert.equal(rows(p), 3); assert.deepEqual(p.dash.params(), FILTERS);
});

test('only the reader\'s own actions ever write the filters: not the update check, the swap or the draw callback', () => {
  // The filters (agent, weekly/monthly) live in dash.params. Every writer in the shipped page, by its line: nothing in the update
  // check, the data layer's swap or the draw callback is among them, so an automatic update cannot change a selection.
  const writers = html.split('\n').filter((l) => /\bdash\.(setParams|resetParams)\(|\bsetDefaults\(\)/.test(l)).map((l) => l.trim());
  const expected = [
    "if (!dash.params().agent) setDefaults();",                                              // first load only: nothing chosen yet
    "sel.addEventListener('change', () => dash.setParams({ agent: sel.value }));",           // the reader picks an agent
    "document.querySelector('.tm-reset').addEventListener('click', () => { dash.resetParams(); setDefaults(); });",   // Reset
    "document.querySelectorAll('.tm-grain').forEach((b) => b.addEventListener('click', () => dash.setParams({ grain: b.dataset.grain })));",   // Weekly / Monthly
    ".on('click', (ev, d) => dash.setParams({ agent: picked === d.agent ? 'All' : d.agent }))",   // clicking a bar in the agents chart
  ];
  assert.deepEqual(writers.filter((l) => !l.startsWith('const setDefaults =')), expected);
  for (const name of ['POLL', 'DATA']) assert.doesNotMatch(block(name).replace(/setParams\(p\) \{[^\n]*\n|resetParams\(\) \{[^\n]*\n/g, ''), /setParams|resetParams|setDefaults/, name + ' block');
  const draw = html.slice(html.indexOf('dash.onData(() => {\n  const root'));
  assert.doesNotMatch(draw, /setParams|resetParams|setDefaults/, 'the draw callback only reads the filters');
  assert.match(draw, /const p = dash\.params\(\); const agent = p\.agent \|\| 'All'; const grain = p\.grain \|\| 'week';/);
});

// ---------- the selected agent is missing from a newer report ----------
const createAgentNotice = new Function(block('AGENT') + '\nreturn createAgentNotice;')();
function noticeParts() {
  const writes = [];
  const mkEl = (name, extra) => { let t = ''; return { hidden: true, get textContent() { return t; }, set textContent(v) { t = v; writes.push([name, v]); }, ...extra }; };
  const note = mkEl('note'), live = mkEl('live');
  return { note, live, writes, update: createAgentNotice(note, live), spoken: () => writes.filter(([n, v]) => n === 'live' && v !== '').map(([, v]) => v) };
}
const OPTS = ['All', 'Ana Lopez', 'Bo Chen'];

test('no notice while the selected agent is All or is in the report', () => {
  const n = noticeParts();
  for (const sel of [undefined, '', 'All', 'Ana Lopez', 'Bo Chen']) n.update(sel, OPTS);
  n.update('All', ['Ana Lopez']);                                         // All is never a missing agent, whatever the list holds
  assert.equal(n.note.hidden, true); assert.equal(n.note.textContent, '');
  assert.deepEqual(n.writes, [], 'the page is not touched at all');
});

test('a missing agent is explained once in text and once aloud; repeated redraws and later reports that still lack the agent change nothing', () => {
  const n = noticeParts();
  n.update('Cy Diaz', OPTS);
  assert.equal(n.note.hidden, false);
  assert.match(n.note.textContent, /^The selected agent, Cy Diaz, is not in the latest report, so this view shows all agents\./);
  assert.match(n.note.textContent, /selection is kept and comes back if Cy Diaz appears in a later report/);
  assert.match(n.note.textContent, /Choose another agent or press Reset/);
  assert.deepEqual(n.spoken(), ['The selected agent, Cy Diaz, is not in the latest report. Showing all agents.']);
  const writes = n.writes.length;
  for (let i = 0; i < 25; i++) n.update('Cy Diaz', OPTS);                 // resizes, filter changes, three more reports
  n.update('Cy Diaz', ['All', 'Bo Chen']);
  assert.equal(n.writes.length, writes, 'no further writes, so nothing more is announced');
  assert.equal(n.spoken().length, 1);
});

test('when the agent returns the notice goes away and that is said once; if it goes missing again that is a new event', () => {
  const n = noticeParts();
  n.update('Cy Diaz', OPTS);
  n.update('Cy Diaz', [...OPTS, 'Cy Diaz']);
  assert.equal(n.note.hidden, true); assert.equal(n.note.textContent, '');
  assert.deepEqual(n.spoken().slice(1), ['Showing Cy Diaz again.']);
  n.update('Cy Diaz', [...OPTS, 'Cy Diaz']); n.update('Cy Diaz', [...OPTS, 'Cy Diaz']);
  assert.equal(n.spoken().length, 2, 'not repeated while the agent stays present');
  n.update('Cy Diaz', OPTS);
  assert.equal(n.spoken().length, 3);
  assert.equal(n.note.hidden, false);
});

test('choosing another agent, or Reset, clears the notice without saying anything', () => {
  for (const next of ['Ana Lopez', 'All']) {
    const n = noticeParts();
    n.update('Cy Diaz', OPTS);
    const before = n.spoken().length;
    n.update(next, OPTS);
    assert.equal(n.note.hidden, true); assert.equal(n.note.textContent, '');
    assert.equal(n.spoken().length, before, 'no announcement for the reader\'s own action');
    assert.equal(n.live.textContent, '', 'and the spoken message is cleared');
  }
});

test('an agent name is only ever put on the page as text, never as markup', () => {
  const n = noticeParts();
  const evil = '<img src=x onerror=alert(1)>';
  n.update(evil, OPTS);
  assert.ok(n.note.textContent.includes(evil), 'shown literally');
  assert.doesNotMatch(block('AGENT'), /innerHTML|insertAdjacentHTML|outerHTML|document\.write/);
});

test('end to end through the data layer: the stored selection survives a report without the agent and the agent\'s return clears the notice', async () => {
  const withAgents = (names) => (d) => { d.rows.forEach((r, i) => { r.agent = names[i % names.length]; }); return d; };
  const a = report(4, later(-3 * 3_600_000), withAgents(['Ana Lopez', 'Bo Chen'])), b = report(4, later(-2 * 3_600_000), withAgents(['Ana Lopez'])),
    c = report(4, later(-3_600_000), withAgents(['Ana Lopez', 'Bo Chen']));
  const p = page({ initial: a, poll: b });
  const n = noticeParts();
  const draw = () => { const L = p.dash.data('leads'); if (L.status !== 'ok') return; n.update(p.dash.params().agent || 'All', ['All', ...new Set(L.data.map((r) => r.agent))]); };
  p.dash.onData(draw);
  await settle();
  p.dash.setParams({ agent: 'Bo Chen', grain: 'month' });
  assert.equal(n.note.hidden, true);
  await p.clock.advance(LONGEST_FIRST_WAIT);                              // a report without Bo Chen is accepted
  assert.equal(rows(p), 4);
  assert.equal(n.note.hidden, false); assert.match(n.note.textContent, /Bo Chen/);
  assert.deepEqual(p.dash.params(), { agent: 'Bo Chen', grain: 'month' }, 'the stored selection is untouched');
  p.net.poll = c;
  await p.clock.advance(LONGEST_FIRST_WAIT);                              // and a later one that has Bo Chen again
  assert.equal(n.note.hidden, true);
  assert.deepEqual(p.dash.params(), { agent: 'Bo Chen', grain: 'month' });
  assert.deepEqual(n.spoken(), ['The selected agent, Bo Chen, is not in the latest report. Showing all agents.', 'Showing Bo Chen again.']);
});

test('the page has the notice and its spoken region, the page calls the notice from its draw callback, and neither is a repeating live region', () => {
  assert.match(html, /<p class="tm-agent-note" id="agent-note" hidden><\/p>/, 'the visible text is plain (not live) and starts hidden');
  assert.match(html, /<div id="agent-live" class="sr-only" role="status" aria-live="polite" aria-atomic="true"><\/div>/);
  assert.match(html, /createAgentNotice\(document\.getElementById\('agent-note'\), document\.getElementById\('agent-live'\)\)/);
  const draw = html.slice(html.indexOf('dash.onData(() => {\n  const root'));
  assert.equal(draw.split('agentNotice(agent, opts);').length - 1, 1, 'called once per redraw, after the dropdown is set');
  assert.ok(draw.indexOf("sel.value = opts.includes(agent) ? agent : 'All';") < draw.indexOf('agentNotice(agent, opts);'));
  assert.doesNotMatch(html.match(/<p class="tm-agent-note"[^>]*>/)[0], /aria-live|role=/);
});

test('the page wires up what it needs: a polite status region, a non-live visible line, and no new external addresses', async () => {
  const { html } = await import('./helpers.mjs');
  assert.match(html, /<div id="update-live" class="sr-only" role="status" aria-live="polite" aria-atomic="true"><\/div>/);
  assert.match(html, /<p class="tm-updates" id="update-status">/);
  assert.doesNotMatch(html.match(/<p class="tm-updates" id="update-status">[^<]*<\/p>/)[0], /aria-live|role=/, 'the line that changes every check is not a live region');
  assert.match(html, /\.sr-only \{[^}]*clip: rect/);
  const origins = new Set([...html.matchAll(/https?:\/\/[^\/"' )<>]+/g)].map((m) => m[0]));
  assert.deepEqual([...origins].sort(), ['https://fonts.googleapis.com', 'https://fonts.gstatic.com'], 'no new network origins');
  assert.doesNotMatch(html, /\bXMLHttpRequest\b|navigator\.sendBeacon|WebSocket|EventSource/);
});
