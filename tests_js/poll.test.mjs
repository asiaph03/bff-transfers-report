// Tests for the dashboard's update check (BFF-POLL block in site/index.html). Run: node --test tests_js/poll.test.mjs
// The block is cut out of the shipped page, so what is tested is exactly what is published. No dependencies.
import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { block, enc, nodeVersion, row, report, resp, Clock, settle, detSubtle, until, INTERVAL, pagesWorld } from './helpers.mjs';

const BffPoll = new Function(block('POLL') + '\nreturn BffPoll;')();
const subtle = webcrypto.subtle;
const FIRST = INTERVAL + 1_500;     // just past the first check when random=0.5 (a wait of exactly the interval) and before any retry that follows it

// A scripted network: routes get (url, init) and return a response, throw, or never settle (hang) until aborted.
function net(route) {
  const calls = [];
  const fetchFn = (url, init) => {
    calls.push({ url, cache: init && init.cache });
    const out = route(url, init, calls.length);
    if (out === 'hang') return new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' }))));
    return out instanceof Error ? Promise.reject(out) : Promise.resolve(out);
  };
  return { fetchFn, calls, summaries: () => calls.filter((c) => c.url.startsWith('summary.json')), datas: () => calls.filter((c) => c.url.startsWith('data.json')) };
}

function make({ current, route, options, visible = true, subtleImpl = detSubtle, random = () => 0.5 }) {
  const clock = new Clock(); const n = net(route);
  const env = { current, applied: [], statuses: [], adopted: [], visible, throwOnApply: false };
  const poller = BffPoll.create({
    fetchFn: n.fetchFn, now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer, random, subtle: subtleImpl,
    isVisible: () => env.visible, options,
    getCurrent: () => env.current,
    adopt: (s) => env.adopted.push(s),
    apply: (d, s) => { if (env.throwOnApply) throw new Error('boom'); env.applied.push({ d, s }); env.current = { version: s.report ? s.report.version : null, refreshed_at: s.refreshed_at }; },
    onStatus: (s) => env.statuses.push(s),
  });
  return { clock, net: n, env, poller };
}
const cur = (r) => ({ version: r.version, refreshed_at: r.summary.refreshed_at });
const kinds = (m) => m.env.statuses.map((s) => s.kind);
const serve = (state) => (url) => (url.startsWith('summary.json') ? resp(state.summary) : resp(state.data));

// ---------- version parity with the build ----------
test('versionOf equals the Python report_version for the same bytes (shared test vectors)', async () => {
  const vectors = { 'hello': 'LPJNul-wow4m6DsqxbninhsW', '': '47DEQpj8HBSa-_TImW-5JCeu', '{"rows":[]}': 'FjOQLGy7ped3Db7Rct91SiUH', 'é': 'SplVfkAzw1Od4utlRyAXytX5' };
  for (const [text, want] of Object.entries(vectors)) {
    const b = enc.encode(text);
    assert.equal(await BffPoll.versionOf(b.buffer, subtle), want);
    assert.equal(nodeVersion(b), want);
    assert.match(want, /^[A-Za-z0-9_-]{24}$/);
  }
});

// ---------- version changes ----------
test('a new version: checks the summary, downloads that version of data.json, verifies it, then replaces what is shown', async () => {
  const oldR = report(3, '2026-10-25T10:00:00+00:00'), newR = report(5, '2026-10-25T10:30:00+00:00');
  const m = make({ current: cur(oldR), route: serve(newR) });
  m.poller.start();
  assert.equal(m.net.calls.length, 0, 'start() only schedules; the first check is one interval later');
  await m.clock.advance(FIRST); await settle();
  assert.equal(m.env.applied.length, 1);
  assert.equal(m.env.applied[0].d.rows.length, 5);
  assert.deepEqual(m.net.calls.map((c) => c.url.split('?')[0]), ['summary.json', 'data.json']);
  assert.match(m.net.calls[1].url, new RegExp('^data\\.json\\?v=' + newR.version + '$'));
  assert.equal(kinds(m).at(-1), 'updated');
  assert.equal(m.env.current.version, newR.version);
});

test('after an update the same version is not downloaded again', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T11:00:00+00:00');
  const m = make({ current: cur(oldR), route: serve(newR) });
  m.poller.start();
  for (let i = 0; i < 6; i++) await m.clock.advance(FIRST);
  assert.equal(m.net.datas().length, 1, 'data.json is fetched once for the new version');
  assert.equal(m.net.summaries().length, 6);
});

// ---------- unchanged versions ----------
test('an unchanged version costs one small summary request per check and never downloads data', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: serve(r) });
  m.poller.start();
  for (let i = 0; i < 10; i++) await m.clock.advance(FIRST);          // ten times (interval + 1.5 s); random=0.5 gives waits of exactly the interval
  assert.equal(m.net.summaries().length, Math.floor((10 * FIRST) / INTERVAL));
  assert.equal(m.net.datas().length, 0);
  assert.equal(m.env.applied.length, 0);
  assert.deepEqual([...new Set(kinds(m))], ['current']);
});

test('the same data republished with the same timestamp but a different hash is a new version', async () => {
  const a = report(3, '2026-10-25T10:00:00+00:00');
  const b = report(3, '2026-10-25T10:00:00+00:00', (d) => { d.rows[0].payout = 99; return d; });
  assert.notEqual(a.version, b.version);
  const m = make({ current: cur(a), route: serve(b) });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(m.env.applied.length, 1);
});

// ---------- caching behaviour ----------
test('the summary is always revalidated at one plain address; data is never stored before it is verified', async () => {
  const r = report(3), n2 = report(4, '2026-10-25T12:00:00+00:00');
  const state = { summary: r.summary, data: r.data };
  const m = make({ current: cur(r), route: serve(state) });
  m.poller.start();
  await m.clock.advance(FIRST);                      // unchanged
  assert.equal(m.net.calls[0].cache, 'no-cache', 'forces revalidation despite max-age=600');
  for (let i = 0; i < 5; i++) await m.clock.advance(FIRST);
  assert.ok(m.net.summaries().length >= 6);
  assert.deepEqual([...new Set(m.net.summaries().map((c) => c.url))], ['summary.json'], 'no changing key: the browser can validate what it already holds');
  assert.ok(m.net.summaries().every((c) => c.cache === 'no-cache'));
  state.summary = n2.summary; state.data = n2.data;
  await m.clock.advance(FIRST);
  assert.equal(m.net.datas()[0].cache, 'no-store');
  assert.ok(m.net.datas()[0].url.includes(n2.version));
});

// ---------- conditional requests, through a model of the browser cache and a Pages-like server ----------
function worldMake(first, options) {
  const clock = new Clock(); const world = pagesWorld(clock);
  world.publish('summary.json', JSON.stringify(first.summary)); world.publish('data.json', JSON.stringify(first.data));
  const calls = []; const route = (url, init) => { calls.push(url); return world.route(url, init); };
  const env = { current: cur(first), applied: [], statuses: [] };
  const poller = BffPoll.create({ fetchFn: async (u, i) => route(u, i), now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer, random: () => 0.5, subtle: detSubtle, isVisible: () => true, options,
    getCurrent: () => env.current, adopt: () => {}, onStatus: (s) => env.statuses.push(s),
    apply: (d, s) => { env.applied.push(s); env.current = { version: s.report.version, refreshed_at: s.refreshed_at }; } });
  const sums = () => world.log.filter((e) => e.url === 'summary.json');
  return { clock, world, env, poller, sums, datas: () => world.log.filter((e) => e.url.startsWith('data.json')) };
}

test('unchanged report: after the first response every check is a conditional request answered 304, and nothing else is fetched', async () => {
  const r = report(3);
  const w = worldMake(r);
  w.poller.start();
  for (let i = 0; i < 8; i++) await w.clock.advance(FIRST);
  const s = w.sums();
  assert.ok(s.length >= 8);
  assert.deepEqual([s[0].status, s[0].sentValidator], [200, false], 'the first request has nothing to validate');
  assert.ok(s.slice(1).every((e) => e.status === 304 && e.sentValidator), 'every later check sends the stored ETag and gets a 304');
  assert.equal(w.datas().length, 0);
  assert.equal(w.env.applied.length, 0);
  assert.deepEqual([...new Set(w.env.statuses.map((x) => x.kind))], ['current'], 'a 304 reaches the page as the same unchanged summary');
});

test('a changed report is detected: the stored ETag no longer matches, the new summary arrives in full, and its data is fetched once', async () => {
  const a = report(3, '2026-10-25T10:00:00+00:00'), b = report(5, '2026-10-25T10:30:00+00:00');
  const w = worldMake(a);
  w.poller.start();
  for (let i = 0; i < 3; i++) await w.clock.advance(FIRST);
  assert.equal(w.env.applied.length, 0);
  w.world.publish('summary.json', JSON.stringify(b.summary)); w.world.publish('data.json', JSON.stringify(b.data));
  await w.clock.advance(FIRST);
  assert.equal(w.env.applied.length, 1);
  assert.equal(w.env.current.version, b.version);
  const last = w.sums().at(-1);
  assert.deepEqual([last.status, last.sentValidator], [200, true], 'validator sent, mismatch, full new summary');
  assert.equal(w.datas().length, 1);
  for (let i = 0; i < 3; i++) await w.clock.advance(FIRST);
  assert.equal(w.datas().length, 1, 'not downloaded again');
  assert.ok(w.sums().slice(-3).every((e) => e.status === 304), 'and back to 304s for the new summary');
});

test('a cache that keeps serving the old summary for a while delays detection but never causes a failure or a wrong display; the new report is found once it is served', async () => {
  const a = report(3, '2026-10-25T10:00:00+00:00'), b = report(5, '2026-10-25T10:30:00+00:00');
  const w = worldMake(a);
  w.poller.start();
  await w.clock.advance(FIRST);
  const lag = 5 * 60_000;                                                  // the edge holds the old files for five more minutes
  w.world.publish('summary.json', JSON.stringify(b.summary), lag); w.world.publish('data.json', JSON.stringify(b.data), lag);
  for (let t = 0; t < lag - 60_000; t += 60_000) await w.clock.advance(60_000);
  assert.equal(w.env.applied.length, 0, 'still being served the old summary');
  assert.equal(w.poller.state().failures, 0, 'a lagging cache is not a failure');
  assert.equal(w.datas().length, 0);
  for (let i = 0; i < 4; i++) await w.clock.advance(FIRST);
  assert.equal(w.env.applied.length, 1, 'found at the first check after the edge serves it');
  assert.equal(w.env.current.version, b.version);
});

test('why the request mode matters: a browser that was allowed to reuse its copy would keep showing the old report for its whole max-age', async () => {
  const a = report(3, '2026-10-25T10:00:00+00:00'), b = report(5, '2026-10-25T10:30:00+00:00');
  const clock = new Clock(); const world = pagesWorld(clock);
  world.publish('summary.json', JSON.stringify(a.summary));
  await world.route('summary.json', { cache: 'default' });
  world.publish('summary.json', JSON.stringify(b.summary));
  clock.t += 120_000;
  const reused = await (await world.route('summary.json', { cache: 'default' })).arrayBuffer();
  assert.deepEqual(JSON.parse(new TextDecoder().decode(reused)), a.summary, 'the default mode serves the stored (old) copy without asking');
  const asked = await (await world.route('summary.json', { cache: 'no-cache' })).arrayBuffer();
  assert.deepEqual(JSON.parse(new TextDecoder().decode(asked)), b.summary, 'no-cache asks, and gets the new one');
});

test('a stale copy of the summary (older report than the one shown) is ignored and never downgrades the display', async () => {
  const newer = report(4, '2026-10-25T12:00:00+00:00'), older = report(3, '2026-10-25T09:00:00+00:00');
  const m = make({ current: cur(newer), route: serve(older) });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'stale-response');
  assert.equal(m.net.datas().length, 0);
  assert.equal(m.env.applied.length, 0);
  assert.equal(m.poller.state().failures, 0, 'a stale answer is not a failure');
});

test('data that does not match its manifest is rejected, then retried quickly with a fresh address; success resets the retries', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T12:00:00+00:00'), stale = report(3);   // the CDN still serves the old data file
  let served = stale.data;
  const m = make({ current: cur(oldR), route: (url) => (url.startsWith('summary.json') ? resp(newR.summary) : resp(served)) });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'mismatch');
  assert.equal(m.env.applied.length, 0, 'nothing is replaced');
  assert.equal(m.clock.timers.length, 1);
  assert.ok(m.clock.timers[0].ms >= 4500 && m.clock.timers[0].ms <= 5500, 'first retry after about 5 s');
  await m.clock.advance(5_500);
  assert.ok(m.net.datas().at(-1).url.endsWith('&r=1'), 'a retry asks for a different address');
  assert.ok(m.clock.timers[0].ms >= 9000 && m.clock.timers[0].ms <= 11000, 'then about 10 s');
  served = newR.data;                                  // the CDN catches up
  await m.clock.advance(11_000);
  assert.equal(m.env.applied.length, 1);
  assert.equal(m.poller.state().mismatch, 0);
  assert.equal(kinds(m).at(-1), 'updated');
});

test('after three mismatches the check counts as a failure and backs off', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T12:00:00+00:00');
  const m = make({ current: cur(oldR), route: (url) => (url.startsWith('summary.json') ? resp(newR.summary) : resp(oldR.data)) });
  m.poller.start();
  await m.clock.advance(FIRST); await m.clock.advance(5_500); await m.clock.advance(11_000); await m.clock.advance(22_000);
  assert.equal(m.poller.state().failures, 1);
  assert.equal(m.poller.state().mismatch, 0);
  assert.equal(m.env.applied.length, 0);
});

// ---------- failed requests, backoff and recovery ----------
test('network errors, HTTP errors and timeouts never touch the display; waits double up to ten minutes', async () => {
  const r = report(3);
  for (const failure of [new TypeError('Failed to fetch'), resp('x', 500), resp('x', 404), 'hang']) {
    const m = make({ current: cur(r), route: () => failure });
    m.poller.start();
    const waits = [];
    for (let i = 0; i < 8; i++) {
      await m.clock.advance(m.clock.timers[0].ms + 1);
      if (failure === 'hang') await m.clock.advance(10_000);     // the request timeout fires
      waits.push(m.clock.timers.length ? m.clock.timers[0].ms : null);
    }
    assert.equal(m.env.applied.length, 0);
    assert.equal(m.env.current.version, r.version, 'the displayed report is unchanged');
    assert.deepEqual(waits.slice(0, 6), [1, 2, 3, 4, 5, 6].map((f) => Math.min(INTERVAL * 2 ** f, 600000)), 'doubling from the interval (formula unchanged), capped at 10 minutes');
    assert.deepEqual(waits.slice(0, 4), [120000, 240000, 480000, 600000], 'with a 60 s interval: 2, 4, 8 minutes, then the 10 minute ceiling');
    assert.ok(waits.slice(6).every((w) => w === 600000));
  }
});

test('a request that never answers is cut off after the timeout and reported as a timeout', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: () => 'hang' });
  m.poller.start(); await m.clock.advance(FIRST); await m.clock.advance(10_500);
  assert.equal(kinds(m).at(-1), 'error');
  assert.equal(m.env.statuses.at(-1).detail, 'the request timed out');
  assert.equal(m.poller.state().inFlight, false);
});

test('trouble is announced once after three failures, and recovery once; the old report stays up throughout', async () => {
  const r = report(3), later = report(4, '2026-10-25T13:00:00+00:00');
  let up = false;
  const m = make({ current: cur(r), route: (url) => (up ? serve(later)(url) : new TypeError('offline')) });
  m.poller.start();
  for (let i = 0; i < 5; i++) await m.clock.advance(m.clock.timers[0].ms + 1);
  const started = m.env.statuses.filter((s) => s.noticeStarted);
  assert.equal(started.length, 1, 'announced exactly once');
  assert.equal(m.env.statuses.filter((s) => s.trouble).length, 3, 'checks 3, 4 and 5 are in the trouble state');
  assert.equal(m.env.applied.length, 0);
  up = true;
  await m.clock.advance(m.clock.timers[0].ms + 1);
  assert.equal(m.env.statuses.filter((s) => s.recovered).length, 1, 'recovery announced once');
  assert.equal(m.env.applied.length, 1, 'and the report published meanwhile is shown');
  assert.equal(m.poller.state().failures, 0);
  await m.clock.advance(FIRST);
  assert.equal(m.env.statuses.filter((s) => s.recovered).length, 1);
});

test('with nothing displayed yet (first load failed) the first valid report is shown', async () => {
  const r = report(3);
  const m = make({ current: null, route: serve(r) });
  m.poller.start(); const out = await m.poller.checkNow();
  assert.equal(out, 'updated');
  assert.equal(m.env.applied.length, 1);
});

// ---------- malformed metadata ----------
test('malformed summaries are rejected without a data download or any change', async () => {
  const r = report(3), good = r.summary;
  const bad = {
    'not json': 'this is not json', 'an array': [], 'null': 'null', 'html page': '<html>captive portal</html>',
    'no refreshed_at': { ...good, refreshed_at: undefined }, 'unreadable refreshed_at': { ...good, refreshed_at: 'yesterday-ish' },
    'report not an object': { ...good, report: 'x' }, 'short version': { ...good, report: { ...good.report, version: 'abc' } },
    'illegal version characters': { ...good, report: { ...good.report, version: '../../etc/passwd/xxxxxxxxxxxxx' } },
    'zero bytes': { ...good, report: { ...good.report, data_bytes: 0 } }, 'fractional bytes': { ...good, report: { ...good.report, data_bytes: 10.5 } },
    'huge bytes': { ...good, report: { ...good.report, data_bytes: 10 ** 9 } }, 'negative rows': { ...good, report: { ...good.report, rows: -1 } },
    'other file': { ...good, report: { ...good.report, data_file: 'https://elsewhere.example/data.json' } },
  };
  for (const [label, body] of Object.entries(bad)) {
    const newer = body && typeof body === 'object' && !Array.isArray(body) ? { ...body, refreshed_at: body.refreshed_at === undefined ? undefined : body.refreshed_at } : body;
    const m = make({ current: { version: 'older-version-aaaaaaaa', refreshed_at: '2026-10-25T09:00:00+00:00' }, route: () => resp(newer) });
    m.poller.start(); await m.clock.advance(FIRST);
    assert.equal(kinds(m).at(-1), 'invalid', label);
    assert.equal(m.net.datas().length, 0, label + ': no data download');
    assert.equal(m.env.applied.length, 0, label);
  }
});

test('an oversized summary is refused', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: () => resp('{"a":"' + 'x'.repeat(70_000) + '"}') });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'error');
  assert.equal(m.env.applied.length, 0);
});

// ---------- malformed or incomplete data ----------
test('data that is incomplete, inconsistent or malformed is never displayed', async () => {
  const oldR = report(3, '2026-10-25T09:00:00+00:00');
  const cases = {
    'invalid json': () => ({ summary: report(4, '2026-10-25T10:00:00+00:00').summary, body: '{"rows": [' }),
    'no rows array': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { d.rows = 'x'; return d; }); return { summary: r.summary, body: r.bytes }; },
    'empty rows': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { d.rows = []; return d; }); return { summary: r.summary, body: r.bytes }; },
    'row missing a field': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { delete d.rows[2].agent; return d; }); return { summary: r.summary, body: r.bytes }; },
    'row field wrong type': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { d.rows[1].closed = '1'; return d; }); return { summary: r.summary, body: r.bytes }; },
    'row not an object': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { d.rows[0] = 7; return d; }); return { summary: r.summary, body: r.bytes }; },
    'unreadable paid_call': () => { const r = report(4, '2026-10-25T10:00:00+00:00', (d) => { d.rows[0].paid_call = 'soon'; return d; }); return { summary: r.summary, body: r.bytes }; },
    'refreshed_at differs from the summary': () => { const a = report(4, '2026-10-25T10:00:00+00:00'), b = report(4, '2026-10-25T10:05:00+00:00'); return { summary: a.summary, body: b.bytes }; },
    'size differs from the manifest': () => { const r = report(4, '2026-10-25T10:00:00+00:00'); return { summary: { ...r.summary, report: { ...r.summary.report, data_bytes: r.bytes.length + 1 } }, body: r.bytes }; },
    'row count differs from the manifest': () => { const r = report(4, '2026-10-25T10:00:00+00:00'); return { summary: { ...r.summary, report: { ...r.summary.report, rows: 99 } }, body: r.bytes }; },
    'same size, different content (hash)': () => { const r = report(4, '2026-10-25T10:00:00+00:00'); const t = new TextDecoder().decode(r.bytes).replace('"payout":32', '"payout":33'); return { summary: r.summary, body: t }; },
    'a captive-portal page': () => ({ summary: report(4, '2026-10-25T10:00:00+00:00').summary, body: '<html>sign in</html>' }),
  };
  for (const [label, build] of Object.entries(cases)) {
    const { summary, body } = build();
    const m = make({ current: cur(oldR), route: (url) => (url.startsWith('summary.json') ? resp(summary) : resp(body)) });
    m.poller.start(); await m.clock.advance(FIRST);
    assert.equal(kinds(m).at(-1), 'mismatch', label);
    assert.equal(m.env.applied.length, 0, label + ': nothing displayed');
    assert.equal(m.env.current.version, oldR.version, label + ': old report still current');
  }
});

test('an oversized data response is refused', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T10:00:00+00:00');
  const m = make({ current: cur(oldR), route: (url) => (url.startsWith('summary.json') ? resp(newR.summary) : resp(new Uint8Array(6_000_000))), });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'error');
  assert.equal(m.env.applied.length, 0);
});

// ---------- older publications without a manifest ----------
test('without a manifest the timestamp decides: unchanged is ignored, newer is downloaded and still fully validated', async () => {
  const a = report(3, '2026-10-25T09:00:00+00:00'), b = report(4, '2026-10-25T10:00:00+00:00');
  const strip = (s) => { const { report: _r, ...rest } = s; return rest; };
  const state = { summary: strip(a.summary), data: a.data };
  const m = make({ current: { version: null, refreshed_at: a.summary.refreshed_at }, route: serve(state) });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'current'); assert.equal(m.net.datas().length, 0);
  state.summary = strip(b.summary); state.data = b.data;
  await m.clock.advance(FIRST);
  assert.equal(m.env.applied.length, 1);
  assert.ok(decodeURIComponent(m.net.datas()[0].url.split('v=')[1]) === b.summary.refreshed_at, 'addressed by timestamp');
  state.summary = { ...strip(report(5, '2026-10-25T11:00:00+00:00').summary) }; state.data = b.data;   // data does not match this summary
  await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'mismatch'); assert.equal(m.env.applied.length, 1);
});

test('a page loaded before the manifest existed learns the version of an unchanged report without downloading it', async () => {
  const r = report(3);
  const m = make({ current: { version: null, refreshed_at: r.summary.refreshed_at }, route: serve(r) });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(m.env.adopted.length, 1);
  assert.equal(m.net.datas().length, 0);
});

test('a browser that cannot hash (insecure context) still checks size, row count, timestamp and structure', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T12:00:00+00:00');
  const m = make({ current: cur(oldR), route: serve(newR), subtleImpl: null });
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(m.env.applied.length, 1);
  const wrongSize = { ...newR.summary, report: { ...newR.summary.report, data_bytes: newR.bytes.length + 1 } };
  const m2 = make({ current: cur(oldR), route: (u) => (u.startsWith('summary.json') ? resp(wrongSize) : resp(newR.data)), subtleImpl: null });
  m2.poller.start(); await m2.clock.advance(FIRST);
  assert.equal(m2.env.applied.length, 0);
});

// ---------- scheduling, visibility, concurrency ----------
test('waits are the interval plus or minus ten percent', async () => {
  const r = report(3);
  for (const [rnd, lo, hi] of [[0, INTERVAL * 0.9, INTERVAL * 0.9], [1, INTERVAL * 1.1, INTERVAL * 1.1], [0.5, INTERVAL, INTERVAL]]) {
    const m = make({ current: cur(r), route: serve(r), random: () => rnd });
    m.poller.start();
    assert.ok(m.clock.timers[0].ms >= lo && m.clock.timers[0].ms <= hi, `random=${rnd}`);
  }
});

test('a hidden tab makes no requests; showing it checks soon; hiding it again stops the timer', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: serve(r), visible: false });
  m.poller.start();
  assert.equal(m.clock.timers.length, 0, 'nothing is scheduled while hidden');
  await m.clock.advance(120_000);
  assert.equal(m.net.calls.length, 0);
  m.env.visible = true; m.poller.visibilityChanged();
  assert.equal(m.clock.timers.length, 1);
  await m.clock.advance(2_100);
  assert.equal(m.net.summaries().length, 1, 'checked right after the tab became visible');
  m.env.visible = false; m.poller.visibilityChanged();
  assert.equal(m.clock.timers.length, 0, 'the schedule was cancelled');
  await m.clock.advance(120_000);
  assert.equal(m.net.summaries().length, 1);
});

test('never two requests at once: a check started while another is running is refused', { timeout: 10_000 }, async () => {
  const r = report(3);
  const release = [];
  const m = make({ current: cur(r), route: () => new Promise((res) => { release.push(() => res(resp(r.summary))); }) });
  m.poller.start();
  const first = m.poller.checkNow();
  let second; m.poller.checkNow().then((v) => { second = v; });       // not awaited: it must answer at once, not wait for the network
  await settle();
  assert.equal(second, 'busy');
  assert.equal(m.net.calls.length, 1, 'only one request is in flight');
  release.forEach((f) => f()); await first; await settle();
  assert.equal(m.net.calls.length, 1);
});

test('stop() cancels everything', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: serve(r) });
  m.poller.start(); m.poller.stop();
  assert.equal(m.clock.timers.length, 0);
  await m.clock.advance(120_000);
  assert.equal(m.net.calls.length, 0);
  assert.equal(await m.poller.checkNow(), 'stopped');
});

test('if showing the new report fails, it is not marked as shown and is tried again', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T12:00:00+00:00');
  const m = make({ current: cur(oldR), route: serve(newR) });
  m.env.throwOnApply = true;
  m.poller.start(); await m.clock.advance(FIRST);
  assert.equal(kinds(m).at(-1), 'error');
  assert.equal(m.env.current.version, oldR.version);
  m.env.throwOnApply = false;
  await m.clock.advance(m.clock.timers[0].ms + 1);
  assert.equal(m.env.applied.length, 1);
});

test('the request rate is bounded: while healthy, an hour of a visible tab makes at most 1.2 requests a minute', async () => {
  const r = report(3);
  const m = make({ current: cur(r), route: serve(r), random: () => 0 });   // random=0 gives the shortest waits (90% of the interval: 54 s)
  m.poller.start();
  for (let i = 0; i < 20; i++) await m.clock.advance(60_000 * 3);
  assert.ok(m.net.calls.length / 60 <= 1.2, `requests per minute: ${m.net.calls.length / 60}`);
});

test('rows are checked for every field the page reads', () => {
  assert.equal(BffPoll.checkRow(row(1)), null);
  for (const k of BffPoll.ROW_KEYS) { const r = row(1); delete r[k]; assert.match(BffPoll.checkRow(r), /missing|unreadable|no lead_id/, k); }
});

test('the same flow works with the real WebCrypto, which finishes on its own schedule', async () => {
  const oldR = report(3), newR = report(4, '2026-10-25T12:00:00+00:00');
  const m = make({ current: cur(oldR), route: serve(newR), subtleImpl: subtle });
  m.poller.start();
  await m.clock.advance(FIRST);
  await until(() => m.env.statuses.length > 0);
  assert.equal(kinds(m).at(-1), 'updated');
  const bad = report(4, '2026-10-25T12:00:00+00:00', (d) => { d.rows[0].payout = 33; return d; });   // same size, different content
  const m2 = make({ current: cur(oldR), route: (u) => (u.startsWith('summary.json') ? resp(newR.summary) : resp(bad.data)), subtleImpl: subtle });
  m2.poller.start();
  await m2.clock.advance(FIRST);
  await until(() => m2.env.statuses.length > 0);
  assert.equal(kinds(m2).at(-1), 'mismatch');
  assert.equal(m2.env.applied.length, 0);
});

test('the polling settings: a 60 second interval while visible; every other setting is as before', () => {
  const d = BffPoll.DEFAULTS;
  assert.equal(d.intervalMs, 60_000);
  assert.deepEqual(
    { maxBackoffMs: d.maxBackoffMs, jitter: d.jitter, requestTimeoutMs: d.requestTimeoutMs, minGapMs: d.minGapMs, retryDelaysMs: d.retryDelaysMs,
      failuresBeforeNotice: d.failuresBeforeNotice, maxSummaryBytes: d.maxSummaryBytes, maxDataBytes: d.maxDataBytes },
    { maxBackoffMs: 600_000, jitter: 0.1, requestTimeoutMs: 10_000, minGapMs: 2_000, retryDelaysMs: [5_000, 10_000, 20_000],
      failuresBeforeNotice: 3, maxSummaryBytes: 65_536, maxDataBytes: 5_242_880 });
});

test('a new report is picked up at the next check: within one interval (plus jitter) of being published, not before', async () => {
  const oldR = report(3, '2026-10-25T10:00:00+00:00'), newR = report(4, '2026-10-25T11:00:00+00:00');
  const state = { summary: oldR.summary, data: oldR.data };
  const m = make({ current: cur(oldR), route: serve(state), random: () => 1 });          // the longest waits (110% of the interval)
  m.poller.start();
  await m.clock.advance(INTERVAL * 1.1 - 1_000);
  state.summary = newR.summary; state.data = newR.data;                                   // published just after a check would have been due...
  assert.equal(m.env.applied.length, 0);
  await m.clock.advance(INTERVAL * 1.1 + 1_000);
  assert.equal(m.env.applied.length, 1, 'seen by the next check, at most one (jittered) interval later');
});
