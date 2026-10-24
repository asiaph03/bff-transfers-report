// Shared helpers for the dashboard tests: cuts blocks out of the shipped page, builds reports, fakes a clock.
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
// A Windows checkout (core.autocrlf=true) has CRLF line endings; the tests match multi-line text, so read the page with LF.
export const html = readFileSync(path.join(here, '..', 'site', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
export const block = (name) => {
  const a = `/* BFF-${name}:BEGIN */`, b = `/* BFF-${name}:END */`;
  const i = html.indexOf(a), j = html.indexOf(b);
  assert.ok(i >= 0 && j > i, `block ${name} not found in site/index.html`);
  return html.slice(i, j + b.length);
};
export const enc = new TextEncoder();
// The page's own polling settings, so tests derive their timing from them instead of hard-coding it.
export const POLL_DEFAULTS = new Function(block('POLL') + '\nreturn BffPoll.DEFAULTS;')();
export const INTERVAL = POLL_DEFAULTS.intervalMs;
export const LONGEST_FIRST_WAIT = Math.ceil(INTERVAL * 1.1);      // interval plus the full 10% jitter (the page uses Math.random)

// ---------- fixtures ----------
export const nodeVersion = (bytes) => createHash('sha256').update(bytes).digest().subarray(0, 18).toString('base64url');
export const row = (i) => ({ lead_id: 'P' + String(i).padStart(3, '0'), paid_call: '2026-10-2' + (i % 5) + 'T10:11:12', week_start: '2026-10-19', month: '2026-10',
  ringba_calls: 1, connected: 1, converted: 0, payout: 32, answered: 1, agent: 'Ana Lopez', in_lender_crm: 1, in_citizen: 1, in_cordoba: 0,
  in_debtblue: 0, closed: i % 2, active: i % 2, close_lender: i % 2 ? 'Citizen' : '', close_status: i % 2 ? 'Enrolled' : '', close_date: '', days_to_close: i % 2 ? 3 : null,
  debt_enrolled: i % 2 ? 12000.5 : null });
export function report(n = 3, refreshed = '2026-10-25T10:00:00+00:00', mutate = (d) => d) {
  const data = mutate({ refreshed_at: refreshed, rows: Array.from({ length: n }, (_, i) => row(i)) });
  const bytes = enc.encode(JSON.stringify(data));
  const version = nodeVersion(bytes);
  const summary = { refreshed_at: refreshed, window: { size: n, label: 'paid leads' }, totals: { leads: n }, warnings: [], stale_after_days: 14,
    report: { version, data_file: 'data.json', data_bytes: bytes.length, rows: data.rows.length } };
  return { data, bytes, version, summary };
}
export const asBytes = (x) => (x instanceof Uint8Array ? x : enc.encode(typeof x === 'string' ? x : JSON.stringify(x)));
export const resp = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, arrayBuffer: async () => { const b = asBytes(body); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); } });

/* A browser cache in front of a Pages-like server, for the conditional-request tests. The server sends an ETag and
   Cache-Control: max-age=600 and looks files up by path (the query string is ignored, as the live edge was observed to do).
   The browser keeps one entry per full URL and follows the request's cache mode: 'no-cache' always asks the server with
   If-None-Match and, on a 304, hands the page the stored body as an ordinary 200 (which is what fetch does); 'no-store' never
   stores; 'default' reuses a copy younger than max-age without asking. publish(path, text, lagMs) makes a new file visible
   only after lagMs, modelling an edge that keeps serving the previous copy for a while. */
export function pagesWorld(clock) {
  const versions = new Map(); const browser = new Map(); const log = [];
  const etagOf = (text) => '"' + createHash('sha1').update(text).digest('hex').slice(0, 12) + '"';
  const edge = (path) => [...(versions.get(path) || [])].reverse().find((v) => v.visibleAt <= clock.now());
  return {
    log,
    publish(path, text, lagMs = 0) {
      if (!versions.has(path)) versions.set(path, []);
      versions.get(path).push({ text, etag: etagOf(text), visibleAt: clock.now() + lagMs });
    },
    route(url, init) {
      const path = url.split('?')[0], mode = init && init.cache, mine = browser.get(url);
      const entry = { url, mode, sentValidator: false, status: null };
      log.push(entry);
      if (mode === 'default' && mine && clock.now() - mine.at < 600_000) { entry.status = 'from browser cache'; return resp(mine.text); }
      const file = edge(path);
      if (!file) { entry.status = 404; return resp('not found', 404); }
      if (mode === 'no-cache' && mine) {
        entry.sentValidator = true;
        if (mine.etag === file.etag) { entry.status = 304; return resp(mine.text); }
      }
      entry.status = 200;
      if (mode !== 'no-store') browser.set(url, { etag: file.etag, text: file.text, at: clock.now() });
      return resp(file.text);
    },
  };
}

export class Clock {
  constructor() { this.t = 1_700_000_000_000; this.timers = []; this.seq = 0; }
  now = () => this.t;
  setTimer = (fn, ms) => { const id = ++this.seq; this.timers.push({ id, at: this.t + ms, ms, fn }); return id; };
  clearTimer = (id) => { this.timers = this.timers.filter((x) => x.id !== id); };
  async advance(ms) {
    const target = this.t + ms;
    for (;;) {
      const due = this.timers.filter((x) => x.at <= target).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      this.timers = this.timers.filter((x) => x !== due); this.t = due.at; due.fn(); await settle();
    }
    this.t = target; await settle();
  }
}
export const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };


// A SHA-256 that finishes within the current microtask queue (the browser's crypto.subtle finishes on a thread pool, so a fake
// clock cannot know when it is done). Same algorithm and output, so hashes are real; used by the flow tests for determinism.
export const detSubtle = { digest: async (alg, buf) => { const d = createHash('sha256').update(Buffer.from(buf)).digest(); return d.buffer.slice(d.byteOffset, d.byteOffset + d.length); } };
// Wait (real time, short polls) until a condition holds: for the tests that run the real crypto.subtle.
export async function until(cond, ms = 3000) {
  const end = Date.now() + ms;
  while (!cond()) { if (Date.now() > end) throw new Error('condition not met in time'); await new Promise((r) => setTimeout(r, 2)); }
}
