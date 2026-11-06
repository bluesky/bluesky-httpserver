// bluesky-httpserver v2 lab console. Talks to the controller's public API through ui/server.py, which forwards
// requests unchanged; the only non-API calls are /lab/* (token minting, process lookup, fault injection).

// ============================================================================ DOM helpers

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  const late = {};
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "value" || k === "checked") late[k] = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  Object.assign(el, late);
  return el;
}

function append(el, children) {
  for (const c of [children].flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
}

function mount(root, ...children) {
  root.replaceChildren();
  append(root, children);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const short = (uid) => (uid ? String(uid).slice(0, 8) : "—");
const uuid = () =>
  crypto.randomUUID
    ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
        (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16),
      );
const pad = (n) => String(n).padStart(2, "0");
const fmtTime = (d) => {
  d = d instanceof Date ? d : new Date(d);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};
const fmtDur = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}:${pad(s % 60)}`;
  return `${Math.floor(s / 3600)}h${pad(Math.floor((s % 3600) / 60))}m`;
};
const usToDate = (us) => new Date(us / 1000);
const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const roleClaim = (role) => `QueueServer.${capital(role)}`;

function jsonHtml(value) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text === undefined) return "";
  return esc(text).replace(
    /("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (m) => {
      let cls = "j-num";
      if (/^"/.test(m)) cls = /:$/.test(m) ? "j-key" : "j-str";
      else if (/true|false/.test(m)) cls = "j-bool";
      else if (/null/.test(m)) cls = "j-null";
      return `<span class="${cls}">${m}</span>`;
    },
  );
}
const jsonBlock = (value) => h("pre", { class: "json", html: jsonHtml(value) });

function paramsShort(params) {
  return Object.entries(params || {})
    .map(([k, v]) => `${k}=${Array.isArray(v) ? `[${v.join(",")}]` : typeof v === "object" ? JSON.stringify(v) : v}`)
    .join("  ");
}

// ============================================================================ state

const ALICE = { sub: "alice", role: "admin" };
const BOB = { sub: "bob", role: "run" };
const CAROL = { sub: "carol", role: "edit" };
const DAVE = { sub: "dave", role: "read" };

const S = {
  info: null,
  persona: JSON.parse(sessionStorage.getItem("persona") || "null") || ALICE,
  tokens: {},
  readiness: null,
  readyStatus: null,
  catalog: null,
  catalogError: null,
  queue: null,
  queueFlash: false,
  lease: undefined,
  leaseTtl: Number(localStorage.getItem("leaseTtl") || 300),
  autoRenew: localStorage.getItem("autoRenew") !== "false",
  renewing: false,
  ops: new Map(),
  attempts: new Map(),
  executions: new Map(),
  execOrder: [],
  liveAttempt: null,
  attemptRuns: {},
  block: null,
  events: [],
  cursor: 0,
  gaps: 0,
  duplicates: 0,
  waiters: new Set(),
  requests: [],
  reqSeq: 0,
  showBackground: false,
  openRequests: new Set(),
  view: sessionStorage.getItem("view") || "console",
  selected: null,
  forms: {},
  jsonMode: false,
  jsonText: "",
  fieldErrors: {},
  formError: null,
  editing: null,
  policy: "stop_on_failure",
  recoveryNote: "",
  historyTab: "operations",
  evFilter: "all",
  openEvents: new Set(),
  drawer: null,
  demoLogs: {},
  demoExtra: {},
  demoRunning: new Set(),
  menuOpen: false,
};

// ============================================================================ rendering scheduler

const dirty = new Set();
let frame = 0;
function invalidate(...parts) {
  for (const p of parts) dirty.add(p);
  if (!frame) frame = requestAnimationFrame(flush);
}
function flush() {
  frame = 0;
  const parts = [...dirty];
  dirty.clear();
  for (const p of parts) {
    try {
      RENDER[p]?.();
    } catch (e) {
      console.error(`render ${p}`, e);
    }
  }
  tick();
}

// ============================================================================ identity and HTTP

async function tokenFor(p) {
  const key = `${p.sub}:${p.role}`;
  const cached = S.tokens[key];
  if (cached && cached.claims.exp - Date.now() / 1000 > 120) return cached.token;
  const r = await fetch("/lab/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sub: p.sub, role: p.role, ttl_seconds: 1800 }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error);
  S.tokens[key] = j;
  invalidate("header");
  return j.token;
}

async function badToken(kind, p) {
  const r = await fetch("/lab/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sub: p.sub, role: p.role, kind }),
  });
  return (await r.json()).token;
}

const whoOf = (p) => ({ label: p.sub, role: p.role });
const MUTATING = new Set(["POST", "PUT", "DELETE"]);

async function api(method, path, opts = {}) {
  const headers = {};
  let who;
  if (opts.anon) who = { label: "no token", role: "none" };
  else if (opts.token) {
    headers.Authorization = `Bearer ${opts.token}`;
    who = { label: opts.tokenLabel || "token", role: "none" };
  } else {
    const p = opts.as || S.persona;
    headers.Authorization = `Bearer ${await tokenFor(p)}`;
    who = whoOf(p);
  }
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.ifMatch) headers["If-Match"] = opts.ifMatch;
  let key = opts.key;
  if (key === undefined && MUTATING.has(method)) key = uuid();
  if (key) headers["Idempotency-Key"] = key;
  const bodyText = opts.body === undefined ? undefined : typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
  return send({ method, path, headers, bodyText, who, background: !!opts.background, label: opts.label });
}

const RESPONSE_HEADERS = ["etag", "x-request-id", "www-authenticate", "content-type"];
function pickHeaders(res) {
  const out = {};
  for (const n of RESPONSE_HEADERS) {
    const v = res.headers.get(n);
    if (v) out[n] = v;
  }
  return out;
}
function result(status, text, headers) {
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  const error = body && typeof body === "object" && body.error && typeof body.error === "object" ? body.error : null;
  return { status, ok: status >= 200 && status < 300, body, error, headers, etag: headers.etag || null };
}

async function send(spec) {
  const t0 = performance.now();
  let r;
  try {
    const res = await fetch(spec.path, { method: spec.method, headers: spec.headers, body: spec.bodyText });
    r = result(res.status, await res.text(), pickHeaders(res));
  } catch (e) {
    r = result(0, JSON.stringify({ error: { code: "network_error", message: String(e) } }), {});
  }
  record(spec, r, performance.now() - t0);
  return r;
}

function record(spec, r, ms) {
  const entry = { id: ++S.reqSeq, at: new Date(), spec, r, ms };
  S.requests.unshift(entry);
  if (S.requests.length > 400) S.requests.length = 400;
  if (!spec.background || S.showBackground) invalidate("inspector");
  return entry;
}

const describe = (r) =>
  r.status === 0 ? "network error" : `${r.status}${r.error ? ` ${r.error.code}` : ""}${r.error?.message ? ` — ${r.error.message}` : ""}`;

const queueEtag = () => (S.queue ? `"qrev-${S.queue.revision}"` : undefined);

// ============================================================================ toasts

const HINTS = {
  unauthenticated: "The controller accepts only access tokens from the configured identity provider.",
  insufficient_role: (r) =>
    `Your token grants ${roleClaim(S.persona.role)}; this needs ${r.error.details?.required_role}. Switch identity at top right.`,
  lease_required: "Only the holder of the control lease may change the queue or run it. Take control first.",
  lease_held: (r) => `${r.error.details?.holder} holds control until ${fmtTime(r.error.details?.expires_at)}.`,
  stale_revision: (r) =>
    `The queue changed after the revision you based this on (now qrev-${r.error.details?.current_revision}). Nothing was written — review the queue and decide again.`,
  dispatch_blocked: "An outcome could not be proven. Nothing runs until someone acknowledges recovery.",
  no_fence_evidence: "RE Manager does not yet prove that nothing is running. Try again once it is idle.",
  execution_active: "A queue execution is already running.",
  queue_empty: "Nothing is queued.",
  operation_not_queued: "That operation is no longer queued (taken to run, cancelled or replaced).",
  idempotency_conflict: "This Idempotency-Key was already used for a different request.",
};

function toast({ kind = "info", title, body, actions = [], ms = 7000 }) {
  const el = h(
    "div",
    { class: `toast ${kind}` },
    h("div", { class: "t-title" }, title),
    body && h("div", { class: "t-body" }, body),
    actions.length > 0 &&
      h(
        "div",
        { class: "t-actions" },
        actions.map((a) =>
          h("button", { class: "btn small", onclick: () => { el.remove(); a.fn(); } }, a.label),
        ),
      ),
  );
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), ms);
}

function toastError(r, what) {
  const code = r.error?.code || `HTTP ${r.status}`;
  const hint = HINTS[r.error?.code];
  const actions = [];
  if (r.error?.code === "lease_required") actions.push({ label: "Take control", fn: () => takeLease() });
  if (r.error?.code === "stale_revision") actions.push({ label: "Refresh queue", fn: () => refreshQueue() });
  toast({
    kind: "err",
    title: h("span", null, `${what}: `, h("code", null, `${r.status} ${code}`)),
    body: [typeof hint === "function" ? hint(r) : hint || r.error?.message || "", r.error?.request_id ? h("div", { class: "dim mono" }, `request ${short(r.error.request_id)}`) : null],
    actions,
  });
}

// ============================================================================ event model

const TERMINAL = new Set(["succeeded", "failed", "aborted", "unknown", "cancelled", "replaced"]);

function opOf(uid) {
  let o = S.ops.get(uid);
  if (!o) {
    o = { uid, operation_id: "?", operation_version: "?", parameters: {}, state: "queued", events: [], attempts: [] };
    S.ops.set(uid, o);
  }
  return o;
}

function ingest(ev) {
  if (ev.event_id <= S.cursor) {
    S.duplicates++;
    return;
  }
  if (S.cursor && ev.event_id !== S.cursor + 1) S.gaps++;
  S.cursor = ev.event_id;
  S.events.push(ev);
  const p = ev.payload || {};
  const t = ev.event_type;
  const at = new Date(ev.timestamp);
  const op = ev.operation_uid ? opOf(ev.operation_uid) : null;
  if (op) op.events.push(ev.event_id);
  const attempt = ev.attempt_uid ? S.attempts.get(ev.attempt_uid) : null;
  const exec = ev.queue_execution_uid ? S.executions.get(ev.queue_execution_uid) : null;

  switch (t) {
    case "operation.submitted":
      Object.assign(op, {
        operation_id: p.operation_id,
        operation_version: p.operation_version,
        parameters: p.parameters,
        descriptor_sha256: p.descriptor_sha256,
        submitted_by: ev.actor_id,
        submitted_at: at,
        state: "queued",
        replaces: p.replaces || null,
        updated_at: at,
      });
      break;
    case "operation.replaced":
      Object.assign(op, { state: "replaced", replaced_by: p.replaced_by, updated_at: at });
      break;
    case "operation.cancelled":
      Object.assign(op, { state: "cancelled", updated_at: at, cancelled_by: ev.actor_id });
      break;
    case "operation.claimed": {
      const a = { uid: ev.attempt_uid, op: ev.operation_uid, qe: ev.queue_execution_uid, state: "claimed", claimed_at: at };
      S.attempts.set(a.uid, a);
      op.attempts.push(a.uid);
      Object.assign(op, { state: "claimed", updated_at: at });
      exec?.attempts.push(a.uid);
      S.liveAttempt = a.uid;
      break;
    }
    case "operation.running":
      if (attempt) Object.assign(attempt, { state: "running", item_uid: p.item_uid, started_at: at });
      Object.assign(op, { state: "running", updated_at: at });
      break;
    case "operation.succeeded":
    case "operation.failed":
    case "operation.aborted":
    case "operation.unknown": {
      const state = t.split(".")[1];
      if (attempt)
        Object.assign(attempt, {
          state,
          ended_at: at,
          diagnostic: p.diagnostic,
          run_uids: p.run_uids || [],
          proven_later: !!p.previous_state,
        });
      Object.assign(op, { state, updated_at: at, diagnostic: p.diagnostic });
      if (S.liveAttempt === ev.attempt_uid) S.liveAttempt = null;
      break;
    }
    case "operation.safe_stop_requested":
      if (attempt) Object.assign(attempt, { safe_stop_at: at, safe_stop_by: ev.actor_id });
      break;
    case "queue_execution.started":
      S.executions.set(ev.queue_execution_uid, {
        uid: ev.queue_execution_uid,
        state: "running",
        policy: p.policy,
        started_by: ev.actor_id,
        started_at: at,
        admitted: p.operation_uids || [],
        attempts: [],
      });
      S.execOrder.push(ev.queue_execution_uid);
      break;
    case "queue_execution.stop_requested":
      if (exec) Object.assign(exec, { state: "stopping", stop_by: ev.actor_id });
      break;
    case "queue_execution.completed":
    case "queue_execution.stopped":
    case "queue_execution.blocked":
      if (exec) Object.assign(exec, { state: t.split(".")[1], ended_at: at });
      if (t.endsWith("blocked")) S.block = { qe: ev.queue_execution_uid, attempt: ev.attempt_uid, since: at };
      break;
    case "worker.fenced":
      if (exec) Object.assign(exec, { fence: { note: p.note, status: p.status, by: ev.actor_id, at } });
      S.block = null;
      break;
    default:
      if (t.startsWith("lease.")) scheduleLeaseRefresh();
  }
  if (!S.queue || ev.queue_revision > S.queue.revision) scheduleQueueRefresh();
  for (const w of S.waiters) {
    if (ev.event_id > w.after && w.pred(ev)) {
      S.waiters.delete(w);
      w.resolve(ev);
    }
  }
  appendEventRow(ev);
  invalidate("exec", "history", "evhead", "drawer", "showcase");
}

function waitForEvent(pred, timeoutMs, after = S.cursor) {
  const found = S.events.find((e) => e.event_id > after && pred(e));
  if (found) return Promise.resolve(found);
  return new Promise((resolve, reject) => {
    const w = { pred, after, resolve };
    S.waiters.add(w);
    setTimeout(() => {
      if (S.waiters.delete(w)) reject(new Error(`no matching event within ${timeoutMs / 1000}s`));
    }, timeoutMs);
  });
}

function activeExecution() {
  for (let i = S.execOrder.length - 1; i >= 0; i--) {
    const e = S.executions.get(S.execOrder[i]);
    if (["running", "stopping", "blocked"].includes(e.state)) return e;
  }
  return null;
}

// ============================================================================ event stream (SSE over fetch)

const stream = { ctrl: null, paused: false, wake: null, state: "connecting", detail: "", opens: 0, liveSince: 0, heartbeat: 0 };

function setStream(state, detail = "") {
  stream.state = state;
  stream.detail = detail;
  invalidate("evhead");
}

async function streamLoop() {
  let backoff = 1000;
  for (;;) {
    if (stream.paused) {
      setStream("paused", "dropped on purpose");
      await new Promise((r) => (stream.wake = r));
      continue;
    }
    const ctrl = new AbortController();
    stream.ctrl = ctrl;
    setStream("connecting");
    try {
      const p = S.persona;
      const headers = { Authorization: `Bearer ${await tokenFor(p)}`, Accept: "text/event-stream" };
      if (S.cursor > 0) headers["Last-Event-ID"] = String(S.cursor);
      const spec = { method: "GET", path: "/api/v2/events/stream", headers, who: whoOf(p), label: "event stream" };
      const t0 = performance.now();
      const res = await fetch(spec.path, { headers, signal: ctrl.signal });
      if (!res.ok) {
        record(spec, result(res.status, await res.text(), pickHeaders(res)), performance.now() - t0);
        throw new Error(`stream refused with ${res.status}`);
      }
      record(spec, result(res.status, '"(text/event-stream: open)"', pickHeaders(res)), performance.now() - t0);
      if (stream.opens++ > 0) addSeparator(`reconnected · Last-Event-ID: ${S.cursor}`);
      stream.liveSince = Date.now();
      setStream("live");
      backoff = 1000;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith(":")) {
              stream.heartbeat = Date.now();
              invalidate("evhead");
            } else if (line.startsWith("data:")) data += line.slice(5);
          }
          if (data) ingest(JSON.parse(data));
        }
      }
      addSeparator("stream closed by the controller (token expiry) · reconnecting");
    } catch (e) {
      if (ctrl.signal.aborted) continue;
      setStream("error", e.message || String(e));
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 10000);
    }
  }
}
const restartStream = () => stream.ctrl?.abort();
function pauseStream() {
  stream.paused = true;
  stream.ctrl?.abort();
}
function resumeStream() {
  stream.paused = false;
  stream.wake?.();
  stream.wake = null;
}

// ============================================================================ data refreshers

let queueTimer = 0;
function scheduleQueueRefresh() {
  clearTimeout(queueTimer);
  queueTimer = setTimeout(refreshQueue, 120);
}
async function refreshQueue() {
  const r = await api("GET", "/api/v2/queue", { background: true });
  if (r.ok) {
    S.queueFlash = S.queue?.revision !== r.body.revision;
    S.queue = r.body;
    invalidate("queue", "exec");
  }
}

let leaseTimer = 0;
function scheduleLeaseRefresh() {
  clearTimeout(leaseTimer);
  leaseTimer = setTimeout(refreshLease, 100);
}
async function refreshLease() {
  const r = await api("GET", "/api/v2/control-lease", { background: true });
  if (r.ok) S.lease = r.body.lease;
  invalidate("header", "queue", "exec");
}

async function refreshCatalog() {
  const r = await api("GET", "/api/v2/catalog", { background: true });
  if (r.ok) {
    const changed = S.catalog?.catalog_sha256 !== r.body.catalog_sha256;
    S.catalog = r.body;
    S.catalogError = null;
    if (!S.selected && r.body.operations.length) {
      const preferred = r.body.operations.find((d) => d.operation_id === "tst.count") || r.body.operations[0];
      S.selected = descKey(preferred);
    }
    if (changed) invalidate("catalog");
  } else {
    S.catalogError = r;
    invalidate("catalog");
  }
}

async function refreshReady() {
  try {
    const res = await fetch("/ready");
    const text = await res.text();
    const body = JSON.parse(text);
    S.readyStatus = res.status;
    S.readiness = res.status === 502 ? null : body;
    S.readyError = res.status === 502 ? body.error?.message : null;
  } catch (e) {
    S.readiness = null;
    S.readyError = String(e);
  }
  if (S.readiness?.catalog === "compatible" && !S.catalog) refreshCatalog();
  invalidate("header", "exec");
}

async function pollAttempt() {
  const uid = S.liveAttempt;
  if (!uid || S.attempts.get(uid)?.state !== "running") return;
  const r = await api("GET", `/api/v2/attempts/${uid}`, { background: true });
  if (r.ok) {
    S.attemptRuns[uid] = r.body.current_run_uids;
    invalidate("exec");
  }
}

// ============================================================================ actions

const isMine = (lease) => lease && lease.holder === S.persona.sub;

async function takeLease() {
  const r = await api("POST", "/api/v2/control-lease", { body: { ttl_seconds: S.leaseTtl } });
  if (r.ok) toast({ kind: "ok", title: `${S.persona.sub} has control`, body: `for ${fmtDur(S.leaseTtl * 1000)}` });
  else toastError(r, "Take control");
  refreshLease();
}
async function renewLease(quietly = false) {
  const r = await api("POST", "/api/v2/control-lease/renew", { body: { ttl_seconds: S.leaseTtl }, background: quietly });
  if (!r.ok && !quietly) toastError(r, "Renew");
  refreshLease();
  return r;
}
async function releaseLease() {
  const r = await api("POST", "/api/v2/control-lease/release");
  if (!r.ok) toastError(r, "Release");
  refreshLease();
}
async function overrideLease() {
  const reason = prompt(`Take control from ${S.lease?.holder}. Reason (recorded in the audit log):`, "");
  if (reason == null) return;
  const r = await api("POST", "/api/v2/control-lease/override", { body: { reason, ttl_seconds: S.leaseTtl } });
  if (r.ok) toast({ kind: "ok", title: `Control taken from ${S.lease?.holder}`, body: `reason: “${reason}”` });
  else toastError(r, "Override");
  refreshLease();
}

async function submitOperation() {
  const desc = selectedDescriptor();
  if (!desc) return;
  let parameters;
  if (S.jsonMode) {
    try {
      parameters = JSON.parse(S.jsonText);
    } catch (e) {
      S.formError = `Not JSON: ${e.message}`;
      invalidate("catalog");
      return;
    }
  } else parameters = buildParameters(desc);
  const body = { operation_id: desc.operation_id, operation_version: desc.operation_version, parameters };
  const editing = S.editing;
  const r = editing
    ? await api("PUT", `/api/v2/operations/${editing}`, { body, ifMatch: queueEtag() })
    : await api("POST", "/api/v2/operations", { body, ifMatch: queueEtag() });
  S.fieldErrors = {};
  S.formError = null;
  if (r.ok) {
    toast({
      kind: "ok",
      title: editing ? `Replaced ${short(editing)} by ${short(r.body.operation_uid)}` : `Queued ${r.body.operation_id}`,
      body: h("span", null, `operation ${short(r.body.operation_uid)} · new revision `, h("code", null, r.etag)),
    });
    S.editing = null;
    refreshQueue();
  } else if (r.error?.code === "invalid_parameters" || r.error?.code === "invalid_request") {
    for (const e of r.error.details?.errors || []) {
      const m = /^\$\.([^.[]+)/.exec(e.path || "");
      const loc = e.loc ? e.loc.filter((x) => x !== "body").join(".") : null;
      const key = m ? m[1] : loc && loc.startsWith("parameters.") ? loc.split(".")[1] : "$";
      (S.fieldErrors[key] ||= []).push(e.message);
    }
    S.formError = `${r.status} ${r.error.code}: ${r.error.message}`;
  } else {
    S.formError = `${r.status} ${r.error?.code || ""}`;
    toastError(r, editing ? "Replace" : "Submit");
  }
  invalidate("catalog", "queue");
}

async function cancelOperation(uid) {
  const r = await api("DELETE", `/api/v2/operations/${uid}`, { ifMatch: queueEtag() });
  if (!r.ok) toastError(r, "Cancel");
  if (S.editing === uid) S.editing = null;
  refreshQueue();
  invalidate("catalog");
}

async function reorder(uids) {
  const r = await api("POST", "/api/v2/queue/reorder", { body: { operation_uids: uids }, ifMatch: queueEtag() });
  if (r.ok) {
    S.queue = r.body;
    invalidate("queue");
  } else {
    toastError(r, "Reorder");
    refreshQueue();
  }
}

async function startExecution() {
  const r = await api("POST", "/api/v2/queue-executions", { body: { policy: S.policy }, ifMatch: queueEtag() });
  if (r.ok) toast({ kind: "ok", title: "Queue execution started", body: `${short(r.body.queue_execution_uid)} · ${r.body.policy}` });
  else toastError(r, "Start");
}
async function stopExecution(uid) {
  const r = await api("POST", `/api/v2/queue-executions/${uid}/stop`, { ifMatch: queueEtag() });
  if (r.ok) toast({ kind: "ok", title: `Execution ${r.body.state}`, body: "Queued operations stay queued." });
  else toastError(r, "Stop after current");
}
async function safeStop(uid) {
  const r = await api("POST", `/api/v2/attempts/${uid}/safe-stop`);
  if (r.ok) toast({ kind: "warn", title: "Safe stop requested", body: "Pausing at the next checkpoint, then aborting." });
  else toastError(r, "Safe stop");
}
async function acknowledgeRecovery() {
  const r = await api("POST", "/api/v2/recovery/acknowledge", { body: { note: S.recoveryNote }, ifMatch: queueEtag() });
  if (r.ok) {
    toast({ kind: "ok", title: "Recovery acknowledged", body: "Dispatch is open again; queued work stayed queued." });
    S.recoveryNote = "";
  } else toastError(r, "Acknowledge recovery");
  invalidate("exec");
}

function editOperation(uid) {
  const op = S.queue?.operations.find((o) => o.operation_uid === uid);
  if (!op) return;
  const desc = S.catalog?.operations.find((d) => d.operation_id === op.operation_id && d.operation_version === op.operation_version);
  if (desc) {
    S.selected = descKey(desc);
    S.forms[S.selected] = rawFromParameters(desc, op.parameters);
  }
  S.jsonText = JSON.stringify(op.parameters, null, 2);
  S.editing = uid;
  S.fieldErrors = {};
  S.formError = null;
  setView("console");
  invalidate("catalog", "queue");
}

// ============================================================================ catalog form

const descKey = (d) => `${d.operation_id}|${d.operation_version}`;
const selectedDescriptor = () => S.catalog?.operations.find((d) => descKey(d) === S.selected);

const PRESETS = {
  "tst.count": [
    ["Quick count", { detectors: ["xas_det"], num: 1 }],
    ["Slow count (≈20 s)", { detectors: ["xas_det", "diag1"], num: 20, delay: 1 }],
  ],
  "bmm.change_energy": [["7 keV, Si(111)", { energy: 7000, crystal: "si111" }]],
  "bmm.scan_energy": [["3-point scan", { detectors: ["xas_det"], energies: [6500, 7000, 7500], crystal: "si111" }]],
};

function fieldKind(name, prop, devices) {
  if (devices[name]) return prop.type === "array" ? "devices-multi" : "devices-one";
  if (prop.enum) return "enum";
  if (prop.type === "array") {
    const it = prop.items?.type;
    if (it === "number" || it === "integer") return "num-list";
    if (it === "string") return "str-list";
    return "json";
  }
  if (prop.type === "integer" || prop.type === "number") return "number";
  if (prop.type === "boolean") return "bool";
  if (prop.type === "string") return "string";
  return "json";
}

function constraint(prop) {
  const bits = [prop.type === "array" ? `array of ${prop.items?.type || "any"}` : prop.type];
  const src = prop.type === "array" ? { ...prop.items, minItems: prop.minItems, maxItems: prop.maxItems } : prop;
  if (src.minimum !== undefined) bits.push(`≥ ${src.minimum}`);
  if (src.exclusiveMinimum !== undefined) bits.push(`> ${src.exclusiveMinimum}`);
  if (src.maximum !== undefined) bits.push(`≤ ${src.maximum}`);
  if (src.exclusiveMaximum !== undefined) bits.push(`< ${src.exclusiveMaximum}`);
  if (src.minItems !== undefined) bits.push(`min ${src.minItems} item${src.minItems === 1 ? "" : "s"}`);
  return bits.filter(Boolean).join(" · ");
}

function rawFromParameters(desc, params) {
  const raw = {};
  const props = desc.parameters_schema.properties || {};
  for (const [name, prop] of Object.entries(props)) {
    if (!(name in params)) continue;
    const v = params[name];
    const kind = fieldKind(name, prop, desc.devices);
    if (kind === "devices-multi") raw[name] = Array.isArray(v) ? [...v] : [v];
    else if (kind === "num-list" || kind === "str-list") raw[name] = Array.isArray(v) ? v.join(", ") : String(v);
    else if (kind === "json") raw[name] = JSON.stringify(v);
    else if (kind === "bool") raw[name] = !!v;
    else raw[name] = String(v);
  }
  return raw;
}

// Converts inputs to JSON types only; the schema is enforced by the controller, so out-of-range values are sent.
function buildParameters(desc) {
  const raw = S.forms[descKey(desc)] || {};
  const props = desc.parameters_schema.properties || {};
  const out = {};
  const num = (s) => (s.trim() !== "" && !Number.isNaN(Number(s)) ? Number(s) : s.trim());
  for (const [name, prop] of Object.entries(props)) {
    const v = raw[name];
    if (v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    switch (fieldKind(name, prop, desc.devices)) {
      case "devices-multi":
        out[name] = v;
        break;
      case "number":
        out[name] = num(v);
        break;
      case "num-list":
        out[name] = v.split(/[\s,]+/).filter(Boolean).map(num);
        break;
      case "str-list":
        out[name] = v.split(/[\s,]+/).filter(Boolean);
        break;
      case "bool":
        out[name] = !!v;
        break;
      case "json":
        try {
          out[name] = JSON.parse(v);
        } catch {
          out[name] = v;
        }
        break;
      default:
        out[name] = v;
    }
  }
  return out;
}

function renderField(desc, name, prop, required) {
  const key = descKey(desc);
  const raw = (S.forms[key] ||= {});
  const set = (v) => {
    raw[name] = v;
  };
  const kind = fieldKind(name, prop, desc.devices);
  let input;
  if (kind === "devices-multi" || kind === "devices-one") {
    const ids = desc.devices[name];
    const multi = kind === "devices-multi";
    const chosen = () => (multi ? raw[name] || [] : raw[name] ? [raw[name]] : []);
    input = h(
      "div",
      { class: "row wrap" },
      ids.map((id) => {
        const b = h(
          "button",
          {
            class: `chip-toggle${chosen().includes(id) ? " on" : ""}`,
            onclick: () => {
              if (multi) {
                const cur = chosen();
                set(cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
              } else set(raw[name] === id ? "" : id);
              invalidate("catalog");
            },
          },
          id,
        );
        return b;
      }),
    );
  } else if (kind === "enum") {
    input = h(
      "select",
      { onchange: (e) => set(e.target.value), value: raw[name] ?? "" },
      h("option", { value: "" }, "—"),
      prop.enum.map((v) => h("option", { value: String(v) }, String(v))),
    );
  } else if (kind === "bool") {
    input = h("input", { type: "checkbox", checked: !!raw[name], onchange: (e) => set(e.target.checked) });
  } else {
    const placeholder = { "num-list": "e.g. 6500, 7000, 7500", "str-list": "a, b, c", json: "JSON" }[kind] || "";
    input = h("input", { type: "text", value: raw[name] ?? "", placeholder, oninput: (e) => set(e.target.value) });
  }
  const errs = S.fieldErrors[name] || [];
  return h(
    "label",
    { class: "field" },
    h(
      "div",
      { class: "row" },
      h("span", { class: "name" }, name, required ? h("span", { class: "req" }, " *") : null),
      h("span", { class: "constraint right" }, desc.devices[name] ? "device id" + (kind === "devices-multi" ? "s" : "") : constraint(prop)),
    ),
    input,
    errs.map((e) => h("div", { class: "field-error" }, e)),
  );
}

// ============================================================================ header

function renderHeader() {
  // tabs
  mount(
    $("#tabs"),
    [
      ["console", "Console"],
      ["showcase", "v1 → v2 showcase"],
    ].map(([id, label]) => h("button", { class: S.view === id ? "active" : "", onclick: () => setView(id) }, label)),
  );

  // readiness
  const R = S.readiness;
  const fieldDot = (ok, warn) => h("span", { class: `dot ${ok ? "ok" : warn ? "warn" : "err"}` });
  if (!R) {
    mount($("#readiness"), h("span", { class: "pill" }, h("span", { class: "dot err pulse" }), S.readyError ? "controller unreachable" : "checking…"));
  } else {
    const good = { worker: "reachable", environment: "present", catalog: "compatible", dispatch: "open" };
    mount(
      $("#readiness"),
      h(
        "span",
        { class: "pill ready-big", title: `GET /ready → ${S.readyStatus}` },
        h("span", { class: `dot ${R.ready ? "ok" : "err"}` }),
        R.ready ? "Ready" : "Not ready",
        h("span", { class: "dim mono" }, ` ${S.readyStatus}`),
      ),
      Object.entries(good).map(([k, ok]) =>
        h("span", { class: "pill", title: `${k}: ready when ${ok}` }, fieldDot(R[k] === ok, R[k] === "unknown" || R[k] === "absent"), h("span", { class: "k" }, k), R[k]),
      ),
    );
  }

  // lease
  const L = S.lease;
  const mine = isMine(L);
  const ttlSelect = h(
    "select",
    {
      title: "Lease time-to-live",
      value: String(S.leaseTtl),
      onchange: (e) => {
        S.leaseTtl = Number(e.target.value);
        localStorage.setItem("leaseTtl", S.leaseTtl);
      },
    },
    [30, 120, 300, 900, 3600].map((s) => h("option", { value: String(s) }, fmtDur(s * 1000))),
  );
  const leasePill = (who, cls) =>
    h(
      "span",
      { class: "pill", title: L ? `lease ${L.lease_uid}\nacquired ${L.acquired_at}\nexpires ${L.expires_at}` : "nobody holds the control lease" },
      h("span", { class: `dot ${cls}` }),
      h("span", { class: "k" }, "control"),
      who,
      L ? h("span", { class: "mono", "data-countdown": new Date(L.expires_at).getTime() }, "") : null,
    );
  if (S.lease === undefined) mount($("#lease"), leasePill("…", ""));
  else if (!L)
    mount(
      $("#lease"),
      leasePill("nobody", ""),
      ttlSelect,
      h("button", { class: "btn small primary", onclick: takeLease, title: "POST /api/v2/control-lease · needs Edit" }, "Take control"),
    );
  else if (mine)
    mount(
      $("#lease"),
      leasePill(h("b", null, "you"), "ok"),
      ttlSelect,
      h("button", { class: "btn small", onclick: () => renewLease() }, "Renew"),
      h("button", { class: "btn small", onclick: releaseLease }, "Release"),
      h(
        "label",
        { class: "row muted", style: { gap: "4px", fontSize: "12px" }, title: "Renew automatically before the lease lapses" },
        h("input", {
          type: "checkbox",
          checked: S.autoRenew,
          onchange: (e) => {
            S.autoRenew = e.target.checked;
            localStorage.setItem("autoRenew", S.autoRenew);
          },
        }),
        "auto",
      ),
    );
  else
    mount(
      $("#lease"),
      leasePill(h("b", null, L.holder), "warn"),
      h("button", { class: "btn small warn", onclick: overrideLease, title: "POST /api/v2/control-lease/override · needs Admin" }, "Override…", h("span", { class: "role-tag" }, "admin")),
    );

  // identity
  const p = S.persona;
  const tok = S.tokens[`${p.sub}:${p.role}`];
  const menu =
    S.menuOpen &&
    h(
      "div",
      { class: "menu", onclick: (e) => e.stopPropagation() },
      h("div", { class: "meta" }, "Sign in as (tokens minted by the lab identity provider)"),
      (S.info?.personas || []).map((q) =>
        h(
          "div",
          { class: "item", onclick: () => switchPersona({ sub: q.sub, role: q.role }) },
          h("span", { class: `avatar r-${q.role}` }, q.sub[0].toUpperCase()),
          h("div", { class: "grow" }, h("div", null, h("b", null, q.sub), " ", h("span", { class: "dim" }, q.title)), h("div", { class: "mono dim" }, roleClaim(q.role))),
          q.sub === p.sub && q.role === p.role ? "✓" : null,
        ),
      ),
      h(
        "div",
        { class: "item", onclick: () => {
          const sub = prompt("Principal (sub claim):", "eve");
          if (!sub) return;
          const role = prompt("Role: read, edit, run or admin", "edit");
          if (!["read", "edit", "run", "admin"].includes(role)) return;
          switchPersona({ sub, role });
        } },
        h("span", { class: "avatar r-none" }, "+"),
        "Someone else…",
      ),
      h("div", { class: "sep" }),
      tok &&
        h(
          "div",
          { class: "meta" },
          h("div", null, "Access token claims (verified by the controller against ", h("code", null, S.info?.issuer + "/jwks.json"), "):"),
          jsonBlock(tok.claims),
          h("div", { class: "row", style: { marginTop: "6px" } },
            h("button", { class: "btn small", onclick: () => navigator.clipboard?.writeText(tok.token).then(() => toast({ title: "Token copied" })) }, "Copy token"),
            h("span", { class: "dim" }, "expires in ", h("span", { class: "mono", "data-countdown": tok.claims.exp * 1000 }, "")),
          ),
        ),
    );
  mount(
    $("#identity"),
    h(
      "div",
      { class: "persona", onclick: (e) => { e.stopPropagation(); S.menuOpen = !S.menuOpen; invalidate("header"); } },
      h("span", { class: `avatar r-${p.role}` }, p.sub[0].toUpperCase()),
      h("div", { class: "who" }, h("b", null, p.sub), h("small", { class: "mono" }, roleClaim(p.role))),
      "▾",
      menu,
    ),
  );
  tick();
}

function switchPersona(p) {
  S.persona = p;
  S.menuOpen = false;
  sessionStorage.setItem("persona", JSON.stringify(p));
  restartStream();
  invalidate("header", "queue", "exec", "catalog");
  toast({ title: `Signed in as ${p.sub}`, body: `${roleClaim(p.role)} · the event stream reconnects with Last-Event-ID, missing nothing.` });
}

function setView(v) {
  S.view = v;
  sessionStorage.setItem("view", v);
  invalidate("header", "view");
}

// ============================================================================ console view

const consoleEls = {};
function renderView() {
  if (S.view === "console") {
    if (!consoleEls.root) {
      consoleEls.catalog = h("div", { class: "card" });
      consoleEls.exec = h("div", { class: "card" });
      consoleEls.queue = h("div", { class: "card" });
      consoleEls.history = h("div", { class: "card" });
      consoleEls.root = h(
        "div",
        { class: "console" },
        h("div", { class: "stack" }, consoleEls.catalog),
        h("div", { class: "stack" }, consoleEls.exec, consoleEls.queue, consoleEls.history),
      );
    }
    mount($("#view"), consoleEls.root);
    renderCatalog();
    renderExec();
    renderQueue();
    renderHistory();
  } else {
    renderShowcase();
  }
}

function renderCatalog() {
  if (S.view !== "console" || !consoleEls.catalog) return;
  const C = S.catalog;
  const head = h(
    "div",
    { class: "card-head" },
    h("h2", null, "Operation catalog"),
    C && h("span", { class: "sha right", title: `catalog_sha256 ${C.catalog_sha256}` }, `sha256 ${C.catalog_sha256.slice(0, 10)}…`),
  );
  if (!C) {
    mount(
      consoleEls.catalog,
      head,
      h("div", { class: "empty" }, S.catalogError ? `GET /api/v2/catalog → ${describe(S.catalogError)}` : "Loading the catalog…"),
    );
    return;
  }
  const desc = selectedDescriptor();
  const list = h(
    "div",
    { class: "op-list" },
    C.operations.map((d) =>
      h(
        "div",
        {
          class: `op-item${descKey(d) === S.selected ? " active" : ""}`,
          onclick: () => {
            S.selected = descKey(d);
            S.fieldErrors = {};
            S.formError = null;
            if (S.jsonMode) S.jsonText = JSON.stringify(buildParameters(d), null, 2);
            invalidate("catalog");
          },
        },
        h("span", { class: "op-id" }, d.operation_id),
        h("span", { class: "ver" }, `v${d.operation_version}`),
        h("span", { class: "sha right", title: `descriptor_sha256 ${d.descriptor_sha256}` }, d.descriptor_sha256.slice(0, 8)),
      ),
    ),
  );
  if (!desc) {
    mount(consoleEls.catalog, head, list);
    return;
  }
  const schema = desc.parameters_schema;
  const required = new Set(schema.required || []);
  const presets = PRESETS[desc.operation_id] || [];
  const form = S.jsonMode
    ? h("textarea", { rows: 9, value: S.jsonText, oninput: (e) => (S.jsonText = e.target.value) })
    : h(
        "div",
        { class: "form-grid" },
        Object.entries(schema.properties || {}).map(([name, prop]) => renderField(desc, name, prop, required.has(name))),
      );
  const editingOp = S.editing && S.queue?.operations.find((o) => o.operation_uid === S.editing);
  mount(
    consoleEls.catalog,
    head,
    list,
    editingOp &&
      h(
        "div",
        { class: "mode-banner" },
        "✎ Replacing queued operation ",
        h("code", null, short(S.editing)),
        h("button", { class: "btn small ghost right", onclick: () => { S.editing = null; invalidate("catalog", "queue"); } }, "cancel edit"),
      ),
    presets.length > 0 &&
      h(
        "div",
        { class: "presets" },
        h("span", { class: "hint" }, "Presets:"),
        presets.map(([label, params]) =>
          h(
            "button",
            {
              class: "btn small",
              onclick: () => {
                S.forms[descKey(desc)] = rawFromParameters(desc, params);
                S.jsonText = JSON.stringify(params, null, 2);
                S.fieldErrors = {};
                S.formError = null;
                invalidate("catalog");
              },
            },
            label,
          ),
        ),
      ),
    form,
    (S.fieldErrors.$ || []).map((e) => h("div", { class: "field-error" }, e)),
    S.formError && h("div", { class: "field-error", style: { marginTop: "8px" } }, S.formError),
    h(
      "div",
      { class: "row", style: { marginTop: "12px" } },
      h(
        "button",
        { class: "btn primary", onclick: submitOperation, title: editingOp ? "PUT /api/v2/operations/{uid} · Edit role + lease" : "POST /api/v2/operations · Edit role + lease" },
        editingOp ? "Replace" : "Add to queue",
        h("span", { class: "role-tag", style: { color: "white", borderColor: "rgba(255,255,255,.4)" } }, "edit"),
      ),
      h("span", { class: "hint" }, "If-Match ", h("code", null, queueEtag() || "—")),
      h(
        "button",
        {
          class: "btn small ghost right",
          onclick: () => {
            if (!S.jsonMode) S.jsonText = JSON.stringify(buildParameters(desc), null, 2);
            else {
              try {
                S.forms[descKey(desc)] = rawFromParameters(desc, JSON.parse(S.jsonText));
              } catch {}
            }
            S.jsonMode = !S.jsonMode;
            invalidate("catalog");
          },
        },
        S.jsonMode ? "Form" : "Edit as JSON",
      ),
    ),
    h(
      "div",
      { class: "hint", style: { marginTop: "10px" } },
      "The form only converts inputs to JSON; the controller validates them against the descriptor's JSON Schema and device map before anything is written. Try an out-of-range value.",
    ),
    h("details", { style: { marginTop: "8px" } }, h("summary", { class: "hint" }, "Descriptor"), jsonBlock(desc)),
  );
}

function renderQueue() {
  if (S.view !== "console" || !consoleEls.queue) return;
  const Q = S.queue;
  const flash = S.queueFlash;
  S.queueFlash = false;
  const active = activeExecution();
  const head = h(
    "div",
    { class: "card-head" },
    h("h2", null, "Queue"),
    Q && h("span", { class: `etag${flash ? " flash" : ""}`, title: "ETag of GET /api/v2/queue — sent as If-Match on every edit" }, `ETag "qrev-${Q.revision}"`),
    h("span", { class: "right hint" }, Q ? `${Q.operations.length} queued · drag to reorder` : ""),
  );
  if (!Q) return mount(consoleEls.queue, head, h("div", { class: "empty" }, "Loading…"));
  let dragUid = null;
  const items = Q.operations.map((op, i) => {
    const el = h(
      "div",
      {
        class: `q-item${S.editing === op.operation_uid ? " editing" : ""}`,
        draggable: "true",
        ondragstart: (e) => {
          dragUid = op.operation_uid;
          el.classList.add("dragging");
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", op.operation_uid);
        },
        ondragend: () => el.classList.remove("dragging"),
        ondragover: (e) => {
          e.preventDefault();
          el.classList.add("drop-before");
        },
        ondragleave: () => el.classList.remove("drop-before"),
        ondrop: (e) => {
          e.preventDefault();
          el.classList.remove("drop-before");
          const from = dragUid || e.dataTransfer.getData("text/plain");
          if (!from || from === op.operation_uid) return;
          const uids = Q.operations.map((o) => o.operation_uid).filter((u) => u !== from);
          uids.splice(uids.indexOf(op.operation_uid), 0, from);
          reorder(uids);
        },
      },
      h("span", { class: "handle" }, "⋮⋮"),
      h("span", { class: "pos" }, i + 1),
      h(
        "div",
        { class: "main" },
        h("div", { class: "row" }, h("span", { class: "op-id" }, op.operation_id), h("span", { class: "ver" }, `v${op.operation_version}`)),
        h("div", { class: "params", title: JSON.stringify(op.parameters) }, paramsShort(op.parameters)),
        h("div", { class: "meta" }, `by ${op.submitted_by} · ${fmtTime(op.submitted_at)} · `, h("span", { class: "mono", style: { cursor: "pointer" }, onclick: () => openOperation(op.operation_uid) }, short(op.operation_uid))),
      ),
      h(
        "div",
        { class: "row" },
        h("button", { class: "btn small ghost", title: "Replace (PUT) · keeps the position", onclick: () => editOperation(op.operation_uid) }, "✎"),
        h("button", { class: "btn small ghost", title: "Cancel (DELETE)", onclick: () => cancelOperation(op.operation_uid) }, "✕"),
      ),
    );
    return el;
  });
  const endZone =
    Q.operations.length > 1 &&
    h(
      "div",
      {
        class: "hint",
        style: { textAlign: "center", padding: "4px", border: "1px dashed transparent", borderRadius: "8px" },
        ondragover: (e) => e.preventDefault(),
        ondrop: (e) => {
          e.preventDefault();
          const from = dragUid || e.dataTransfer.getData("text/plain");
          if (!from) return;
          reorder([...Q.operations.map((o) => o.operation_uid).filter((u) => u !== from), from]);
        },
      },
      "drop here to move last",
    );
  mount(
    consoleEls.queue,
    head,
    active && active.state === "running" && Q.operations.length > 0 && h("div", { class: "hint", style: { marginBottom: "8px" } }, `Execution ${short(active.uid)} is running: it takes these in order, including anything added now.`),
    Q.operations.length ? h("div", { class: "queue-list" }, items, endZone) : h("div", { class: "empty" }, "The queue is empty. Add an operation from the catalog."),
  );
}

function renderExec() {
  if (S.view !== "console" || !consoleEls.exec) return;
  const active = activeExecution();
  const R = S.readiness;
  const blocked = S.block || R?.dispatch === "blocked";
  const head = h(
    "div",
    { class: "card-head" },
    h("h2", null, "Execution"),
    active && h("span", { class: `state s-${active.state}` }, active.state),
    h("span", { class: "right hint" }, active ? `policy ${active.policy}` : ""),
  );
  const parts = [head];

  if (blocked) {
    const a = S.block?.attempt ? S.attempts.get(S.block.attempt) : null;
    const op = a ? S.ops.get(a.op) : null;
    parts.push(
      h(
        "div",
        { class: "blocked" },
        h("h3", null, "Dispatch blocked — an outcome could not be proven"),
        a
          ? h(
              "div",
              { class: "muted" },
              `Attempt ${short(a.uid)} of `,
              h("code", null, op?.operation_id || "?"),
              ` ended `,
              h("span", { class: "state s-unknown" }, a.state),
              a.diagnostic ? ` — ${a.diagnostic}` : "",
              a.proven_later ? " (RE Manager later proved its outcome; the block still needs an acknowledgement)" : "",
            )
          : h("div", { class: "muted" }, "GET /ready reports dispatch: blocked."),
        h(
          "div",
          { class: "hint", style: { margin: "8px 0" } },
          "Nothing runs until an operator with the Run role who holds control checks the instrument and acknowledges. The controller accepts only while RE Manager proves nothing is running (fence evidence), records the note and that evidence as ",
          h("code", null, "worker.fenced"),
          ", stops the blocked execution and reopens the worker environment if needed. Queued operations stay queued.",
        ),
        h("textarea", {
          rows: 2,
          placeholder: "What did you check? (required, recorded in the audit log)",
          value: S.recoveryNote,
          oninput: (e) => (S.recoveryNote = e.target.value),
        }),
        h(
          "div",
          { class: "row", style: { marginTop: "8px" } },
          h("button", { class: "btn warn", onclick: acknowledgeRecovery, title: "POST /api/v2/recovery/acknowledge · Run role + lease + If-Match" }, "Acknowledge recovery", h("span", { class: "role-tag" }, "run")),
          h("span", { class: "hint" }, `worker ${R?.worker ?? "?"} · environment ${R?.environment ?? "?"}`),
        ),
      ),
    );
  }

  const live = S.liveAttempt ? S.attempts.get(S.liveAttempt) : null;
  if (active && active.state !== "blocked") {
    parts.push(
      h(
        "div",
        { class: "exec-grid" },
        kv("execution", h("span", { style: { cursor: "pointer" }, onclick: () => openExecution(active.uid) }, short(active.uid))),
        kv("started by", active.started_by),
        kv("running for", h("span", { "data-elapsed": active.started_at.getTime() }, "")),
        kv("attempts", String(active.attempts.length)),
      ),
    );
  }
  if (live) {
    const op = S.ops.get(live.op);
    const runs = S.attemptRuns[live.uid];
    parts.push(
      h(
        "div",
        { class: "attempt" },
        h(
          "div",
          { class: "row" },
          h("span", { class: `state s-${live.state}` }, live.state),
          h("span", { class: "op-id", style: { cursor: "pointer" }, onclick: () => openOperation(live.op) }, op?.operation_id),
          h("span", { class: "ver" }, `v${op?.operation_version}`),
          h("span", { class: "mono muted", "data-elapsed": (live.started_at || live.claimed_at).getTime() }, ""),
          live.safe_stop_at && h("span", { class: "state s-stopping" }, `safe stop requested by ${live.safe_stop_by}`),
          h(
            "div",
            { class: "right row" },
            h("button", { class: "btn small danger", onclick: () => safeStop(live.uid), title: "POST /api/v2/attempts/{uid}/safe-stop · Run role + lease · no If-Match, so it never fails on a stale revision" }, "Safe stop", h("span", { class: "role-tag" }, "run")),
          ),
        ),
        h("div", { class: "params mono muted", style: { marginTop: "6px" } }, paramsShort(op?.parameters)),
        h(
          "div",
          { class: "exec-grid" },
          kv("attempt", live.uid),
          kv("RE Manager item", live.item_uid || "—"),
          kv("Bluesky runs", runs?.length ? runs.map(short).join(", ") : "—"),
        ),
        h("div", { class: "progress" }, h("div")),
      ),
    );
  }
  if (active && ["running", "stopping"].includes(active.state)) {
    parts.push(
      h(
        "div",
        { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "btn", disabled: active.state !== "running", onclick: () => stopExecution(active.uid), title: "POST /api/v2/queue-executions/{uid}/stop · Run role + lease + If-Match" }, "Stop after current", h("span", { class: "role-tag" }, "run")),
        h("span", { class: "hint" }, active.state === "stopping" ? "Stopping once the current operation ends; the rest stays queued." : "Admitted work keeps running even if the lease lapses or this browser closes."),
      ),
    );
  } else if (!blocked) {
    const queued = S.queue?.operations.length || 0;
    parts.push(
      h(
        "div",
        { class: "row wrap" },
        h(
          "div",
          { class: "policy", title: "What a failed operation does to the execution. unknown and interrupted outcomes always block." },
          ["stop_on_failure", "continue_on_failure"].map((p) => h("button", { class: S.policy === p ? "on" : "", onclick: () => { S.policy = p; invalidate("exec"); } }, p)),
        ),
        h("button", { class: "btn primary", onclick: startExecution, title: "POST /api/v2/queue-executions · Run role + lease + If-Match" }, "▶ Start queue execution", h("span", { class: "role-tag", style: { color: "white", borderColor: "rgba(255,255,255,.4)" } }, "run")),
        h("span", { class: "hint" }, `${queued} queued · idle`),
      ),
    );
  }
  mount(consoleEls.exec, parts);
}

const kv = (k, v) => h("div", { class: "kv" }, h("div", { class: "k" }, k), h("div", { class: "v", title: typeof v === "string" ? v : "" }, v));

function renderHistory() {
  if (S.view !== "console" || !consoleEls.history) return;
  const tabs = h(
    "div",
    { class: "policy right" },
    [
      ["operations", "Operations"],
      ["executions", "Queue executions"],
    ].map(([id, label]) => h("button", { class: S.historyTab === id ? "on" : "", onclick: () => { S.historyTab = id; invalidate("history"); } }, label)),
  );
  const head = h("div", { class: "card-head" }, h("h2", null, "History"), h("span", { class: "hint" }, "from the event log"), tabs);
  let body;
  if (S.historyTab === "operations") {
    const ops = [...S.ops.values()].filter((o) => TERMINAL.has(o.state)).sort((a, b) => b.updated_at - a.updated_at).slice(0, 60);
    body = ops.length
      ? h(
          "table",
          { class: "list" },
          h("tr", null, ["state", "operation", "parameters", "by", "attempts", "ended"].map((c) => h("th", null, c))),
          ops.map((o) =>
            h(
              "tr",
              { class: "click", onclick: () => openOperation(o.uid) },
              h("td", null, h("span", { class: `state s-${o.state}` }, o.state)),
              h("td", null, h("span", { class: "op-id" }, o.operation_id), " ", h("span", { class: "ver" }, `v${o.operation_version}`)),
              h("td", { class: "mono muted", style: { maxWidth: "280px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, paramsShort(o.parameters)),
              h("td", null, o.submitted_by),
              h("td", { class: "mono" }, o.attempts.length),
              h("td", { class: "mono dim" }, fmtTime(o.updated_at)),
            ),
          ),
        )
      : h("div", { class: "empty" }, "No finished operations yet.");
  } else {
    const execs = [...S.execOrder].reverse().slice(0, 40).map((u) => S.executions.get(u));
    body = execs.length
      ? h(
          "table",
          { class: "list" },
          h("tr", null, ["state", "execution", "policy", "started by", "attempts", "started", "ended"].map((c) => h("th", null, c))),
          execs.map((e) =>
            h(
              "tr",
              { class: "click", onclick: () => openExecution(e.uid) },
              h("td", null, h("span", { class: `state s-${e.state}` }, e.state)),
              h("td", { class: "mono" }, short(e.uid)),
              h("td", { class: "mono muted" }, e.policy),
              h("td", null, e.started_by),
              h("td", { class: "mono" }, e.attempts.length),
              h("td", { class: "mono dim" }, fmtTime(e.started_at)),
              h("td", { class: "mono dim" }, e.ended_at ? fmtTime(e.ended_at) : "—"),
            ),
          ),
        )
      : h("div", { class: "empty" }, "No queue executions yet.");
  }
  mount(consoleEls.history, head, body);
}

// ============================================================================ drawer (operation / execution detail)

async function openOperation(uid) {
  S.drawer = { kind: "op", uid, record: null, attempts: {} };
  renderDrawer();
  const r = await api("GET", `/api/v2/operations/${uid}`);
  if (S.drawer?.uid !== uid) return;
  S.drawer.record = r;
  for (const a of S.ops.get(uid)?.attempts || []) {
    const ar = await api("GET", `/api/v2/attempts/${a}`);
    if (S.drawer?.uid !== uid) return;
    S.drawer.attempts[a] = ar;
  }
  renderDrawer();
}

async function openExecution(uid) {
  S.drawer = { kind: "qe", uid, record: null };
  renderDrawer();
  const r = await api("GET", `/api/v2/queue-executions/${uid}`);
  if (S.drawer?.uid !== uid) return;
  S.drawer.record = r;
  renderDrawer();
}

function closeDrawer() {
  S.drawer = null;
  renderDrawer();
}

function timeline(events) {
  return h(
    "div",
    { class: "timeline" },
    events.map((ev) =>
      h(
        "div",
        { class: "tl" },
        h(
          "div",
          { class: "t1" },
          h("code", { class: "dim" }, `#${ev.event_id}`),
          h("b", { class: "mono" }, ev.event_type),
          actorChip(ev),
          h("span", { class: "rev" }, `rev ${ev.queue_revision}`),
          h("span", { class: "right dim mono" }, fmtTime(ev.timestamp)),
        ),
        Object.keys(ev.payload || {}).length > 0 && h("div", { class: "muted mono", style: { fontSize: "11.5px", wordBreak: "break-all" } }, JSON.stringify(ev.payload)),
      ),
    ),
  );
}

function renderDrawer() {
  const D = S.drawer;
  const el = $("#drawer");
  if (!D) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const close = h("button", { class: "btn small ghost right", onclick: closeDrawer }, "✕");
  if (D.kind === "op") {
    const op = S.ops.get(D.uid);
    const events = S.events.filter((e) => e.operation_uid === D.uid);
    mount(
      el,
      h("div", { class: "d-head" }, h("h3", { class: "mono" }, op?.operation_id || "operation"), op && h("span", { class: `state s-${op.state}` }, op.state), close),
      h(
        "div",
        { class: "d-body" },
        h(
          "div",
          { class: "exec-grid" },
          kv("operation uid", D.uid),
          kv("submitted by", op?.submitted_by || "?"),
          kv("version", op?.operation_version || "?"),
          kv("descriptor", op?.descriptor_sha256 ? op.descriptor_sha256.slice(0, 16) : "?"),
          op?.replaces && kv("replaces", h("span", { style: { cursor: "pointer" }, onclick: () => openOperation(op.replaces) }, short(op.replaces))),
          op?.replaced_by && kv("replaced by", h("span", { style: { cursor: "pointer" }, onclick: () => openOperation(op.replaced_by) }, short(op.replaced_by))),
        ),
        h("div", null, h("div", { class: "hint" }, "Audit trail (every event naming this operation)"), timeline(events)),
        h("div", null, h("div", { class: "hint" }, `GET /api/v2/operations/${short(D.uid)}… → `, D.record ? describe(D.record) : "…"), D.record && jsonBlock(D.record.body)),
        Object.entries(D.attempts).map(([a, r]) => h("div", null, h("div", { class: "hint" }, `GET /api/v2/attempts/${short(a)}… → ${describe(r)}`), jsonBlock(r.body))),
      ),
    );
  } else {
    const exec = S.executions.get(D.uid);
    const events = S.events.filter((e) => e.queue_execution_uid === D.uid);
    mount(
      el,
      h("div", { class: "d-head" }, h("h3", null, "Queue execution ", h("code", null, short(D.uid))), exec && h("span", { class: `state s-${exec.state}` }, exec.state), close),
      h(
        "div",
        { class: "d-body" },
        exec?.fence && h("div", { class: "lab-only" }, `Recovered by ${exec.fence.by}: “${exec.fence.note}”`),
        h("div", null, h("div", { class: "hint" }, "Audit trail"), timeline(events)),
        h("div", null, h("div", { class: "hint" }, `GET /api/v2/queue-executions/${short(D.uid)}… → `, D.record ? describe(D.record) : "…"), D.record && jsonBlock(D.record.body)),
        exec?.fence && h("div", null, h("div", { class: "hint" }, "Fence evidence recorded with the acknowledgement"), jsonBlock(exec.fence.status)),
      ),
    );
  }
}

// ============================================================================ events panel

const EVENT_CLASS = (t) => {
  if (["operation.submitted", "operation.replaced", "operation.cancelled", "queue.reordered"].includes(t)) return "c-queue";
  if (["operation.claimed", "operation.running", "operation.safe_stop_requested"].includes(t)) return "c-attempt";
  if (["operation.succeeded", "queue_execution.completed"].includes(t)) return "c-good";
  if (["operation.failed", "operation.aborted", "operation.unknown", "queue_execution.blocked"].includes(t)) return "c-bad";
  if (t.startsWith("queue_execution.")) return "c-exec";
  if (t.startsWith("lease.")) return "c-lease";
  return "c-recovery";
};
const FILTERS = {
  all: () => true,
  queue: (t) => EVENT_CLASS(t) === "c-queue",
  run: (t) => t.startsWith("queue_execution.") || /^operation\.(claimed|running|succeeded|failed|aborted|unknown|safe_stop)/.test(t),
  lease: (t) => t.startsWith("lease."),
  recovery: (t) => ["operation.unknown", "queue_execution.blocked", "worker.fenced"].includes(t),
};

function actorChip(ev) {
  if (ev.actor_kind === "principal") return h("span", { class: "actor", title: "principal (from the verified token)" }, ev.actor_id);
  return h("span", { class: `actor ${ev.actor_kind}`, title: `${ev.actor_kind} ${ev.actor_id}` }, ev.actor_kind);
}

function summarize(ev) {
  const p = ev.payload || {};
  const op = ev.operation_uid ? S.ops.get(ev.operation_uid) : null;
  const name = op ? op.operation_id : "";
  switch (ev.event_type) {
    case "operation.submitted":
      return `${p.operation_id} ${paramsShort(p.parameters)}${p.replaces ? ` · replaces ${short(p.replaces)}` : ""}`;
    case "operation.replaced":
      return `${name} → ${short(p.replaced_by)}`;
    case "queue.reordered":
      return `${p.operation_uids?.length} operations`;
    case "operation.claimed":
      return `${name} · attempt ${short(ev.attempt_uid)}`;
    case "operation.running":
      return `${name} · item ${short(p.item_uid)}`;
    case "operation.succeeded":
    case "operation.failed":
    case "operation.aborted":
    case "operation.unknown":
      return [name, p.run_uids?.length ? `${p.run_uids.length} run${p.run_uids.length > 1 ? "s" : ""}` : null, p.diagnostic, p.previous_state ? "proven after unknown" : null].filter(Boolean).join(" · ");
    case "queue_execution.started":
      return `${p.policy} · ${p.operation_uids?.length} admitted`;
    case "lease.acquired":
    case "lease.renewed":
      return `until ${fmtTime(usToDate(p.expires_at))}`;
    case "lease.overridden":
      return `from ${p.previous_holder ?? "nobody"} · “${p.reason}”`;
    case "worker.fenced":
      return `“${p.note}”`;
    default:
      return name || (ev.queue_execution_uid ? `execution ${short(ev.queue_execution_uid)}` : "");
  }
}

let evList = null;
function eventRow(ev, fresh) {
  const open = S.openEvents.has(ev.event_id);
  const row = h(
    "div",
    {
      class: `ev ${EVENT_CLASS(ev.event_type)}${fresh ? " new" : ""}`,
      onclick: () => {
        if (S.openEvents.has(ev.event_id)) S.openEvents.delete(ev.event_id);
        else S.openEvents.add(ev.event_id);
        row.replaceWith(eventRow(ev, false));
      },
    },
    h("span", { class: "id" }, `#${ev.event_id}`),
    h("div", { class: "line1" }, h("span", { class: "type" }, ev.event_type), h("span", { class: "time" }, fmtTime(ev.timestamp))),
    h("div", { class: "line2" }, actorChip(ev), h("span", { class: "rev", title: "queue revision after this event" }, `rev ${ev.queue_revision}`), h("span", { class: "summary" }, summarize(ev))),
    open && jsonBlock(ev),
  );
  return row;
}

function appendEventRow(ev) {
  if (!evList || !FILTERS[S.evFilter](ev.event_type)) return;
  evList.prepend(eventRow(ev, stream.liveSince && Date.now() - stream.liveSince > 1500));
  while (evList.childElementCount > 500) evList.lastElementChild.remove();
}

function addSeparator(text) {
  evList?.prepend(h("div", { class: "ev-sep" }, `— ${text} —`));
}

function rebuildEventList() {
  const rows = [];
  for (let i = S.events.length - 1; i >= 0 && rows.length < 500; i--) {
    if (FILTERS[S.evFilter](S.events[i].event_type)) rows.push(eventRow(S.events[i], false));
  }
  mount(evList, rows);
}

let evHead = null;
function buildEventsPanel() {
  evHead = h("div", { class: "ev-head" });
  evList = h("div", { id: "ev-list" });
  mount($("#events"), evHead, evList);
  renderEvHead();
}

function renderEvHead() {
  if (!evHead) return;
  const st = stream.state;
  const dot = { live: "ok pulse", connecting: "warn pulse", paused: "warn", error: "err" }[st] || "";
  mount(
    evHead,
    h(
      "div",
      { class: "row1" },
      h("h2", null, "Event log"),
      h("span", { class: "pill", title: stream.detail || "" }, h("span", { class: `dot ${dot}` }), st === "live" ? "SSE live" : st),
      h(
        "button",
        {
          class: "btn small right",
          title: "Abort the stream, then reconnect with Last-Event-ID",
          onclick: () => {
            if (stream.paused) resumeStream();
            else pauseStream();
          },
        },
        stream.paused ? "Reconnect" : "Drop stream",
      ),
    ),
    h(
      "div",
      { class: "ev-stats" },
      h("span", null, "cursor ", h("b", null, `#${S.cursor}`)),
      h("span", null, "events ", h("b", null, S.events.length)),
      h("span", { title: "event ids must be consecutive" }, "gaps ", h("b", { style: { color: S.gaps ? "var(--err)" : "var(--ok)" } }, S.gaps)),
      h("span", null, "connections ", h("b", null, stream.opens)),
      stream.heartbeat ? h("span", null, "heartbeat ", h("b", { "data-ago": stream.heartbeat }, "")) : null,
    ),
    h(
      "div",
      { class: "ev-filters" },
      Object.keys(FILTERS).map((f) =>
        h("button", { class: `chip-toggle${S.evFilter === f ? " on" : ""}`, onclick: () => { S.evFilter = f; rebuildEventList(); renderEvHead(); } }, f),
      ),
    ),
  );
  tick();
}

// ============================================================================ inspector

function renderInspector() {
  const root = $("#inspector");
  const collapsed = document.body.classList.contains("inspector-collapsed");
  const visible = S.requests.filter((e) => S.showBackground || !e.spec.background);
  const toggle = () => {
    document.body.classList.toggle("inspector-collapsed");
    renderInspector();
  };
  const head = h(
    "div",
    { class: "insp-head" },
    h("h2", { onclick: toggle }, `${collapsed ? "▸" : "▾"} HTTP inspector`),
    h("span", { class: "hint" }, "every request this page sent, with the v2 headers that make it safe: Authorization, If-Match, Idempotency-Key → ETag, X-Request-ID"),
    h(
      "label",
      { class: "row hint right", style: { gap: "4px" } },
      h("input", { type: "checkbox", checked: S.showBackground, onchange: (e) => { S.showBackground = e.target.checked; renderInspector(); } }),
      "show background reads",
    ),
    h("button", { class: "btn small ghost", onclick: () => { S.requests = []; renderInspector(); } }, "clear"),
  );
  if (collapsed) return mount(root, head);
  const rows = [];
  for (const e of visible.slice(0, 150)) {
    const { spec, r } = e;
    const open = S.openRequests.has(e.id);
    rows.push(
      h(
        "tr",
        { class: "req", onclick: () => { open ? S.openRequests.delete(e.id) : S.openRequests.add(e.id); renderInspector(); } },
        h("td", { class: "dim" }, e.id),
        h("td", { class: "dim" }, fmtTime(e.at)),
        h("td", null, h("span", { class: `avatar r-${spec.who.role}`, style: { width: "16px", height: "16px", display: "inline-grid", fontSize: "9px", marginRight: "5px", verticalAlign: "middle" } }, spec.who.label[0]?.toUpperCase()), spec.who.label),
        h("td", null, h("span", { class: `method m-${spec.method}` }, spec.method), " ", spec.path),
        h("td", null, h("span", { class: `status st-${String(r.status)[0]} st-${r.status}` }, r.status || "ERR")),
        h("td", null, r.error?.code || ""),
        h("td", { class: "dim" }, spec.headers["If-Match"] || ""),
        h("td", null, r.etag || ""),
        h("td", { class: "dim", title: spec.headers["Idempotency-Key"] || "" }, spec.headers["Idempotency-Key"] ? short(spec.headers["Idempotency-Key"]) : ""),
        h("td", { class: "dim", title: r.headers["x-request-id"] || "" }, short(r.headers["x-request-id"])),
        h("td", { class: "dim" }, `${Math.round(e.ms)}ms`),
        h("td", { class: "dim" }, spec.label || ""),
      ),
    );
    if (open) rows.push(h("tr", { class: "detail" }, h("td", { colspan: 12 }, requestDetail(e))));
  }
  const table = h(
    "table",
    { class: "insp-table" },
    h("tr", null, ["#", "time", "as", "request", "status", "code", "If-Match", "ETag", "Idem-Key", "Request-ID", "", ""].map((c) => h("th", null, c))),
    rows,
  );
  mount(root, head, h("div", { class: "insp-body" }, visible.length ? table : h("div", { class: "empty", style: { margin: "10px" } }, "No requests yet.")));
}

function headerLines(headers, highlight) {
  return h(
    "div",
    { class: "hdrs" },
    Object.entries(headers).map(([k, v]) => {
      let shown = v;
      if (k.toLowerCase() === "authorization") shown = `${v.slice(0, 28)}…  (${v.length - 7} chars)`;
      return h("div", { class: highlight.includes(k.toLowerCase()) ? "hl" : "" }, h("span", { class: "hn" }, `${k}: `), shown);
    }),
  );
}

function requestDetail(e) {
  const { spec, r } = e;
  const curl = () => {
    const base = S.info?.controller_url || "";
    const parts = [`curl -i -X ${spec.method} '${base}${spec.path}'`];
    for (const [k, v] of Object.entries(spec.headers)) parts.push(`-H '${k}: ${v}'`);
    if (spec.bodyText) parts.push(`--data '${spec.bodyText.replace(/'/g, "'\\''")}'`);
    navigator.clipboard?.writeText(parts.join(" \\\n  ")).then(() => toast({ title: "curl command copied", body: "Sent straight to the controller, with this request's token." }));
  };
  const replay = (sameKey) => async (ev) => {
    ev.stopPropagation();
    const headers = { ...spec.headers };
    if (!sameKey && headers["Idempotency-Key"]) headers["Idempotency-Key"] = uuid();
    const again = await send({ ...spec, headers, label: `${sameKey ? "replay" : "resend"} of #${e.id}` });
    toast({
      kind: again.ok ? "ok" : "warn",
      title: `${sameKey ? "Replayed with the same Idempotency-Key" : "Resent with a new key"} → ${describe(again)}`,
      body: sameKey && MUTATING.has(spec.method) ? "A performed mutation is never repeated: the stored response is returned." : "",
    });
  };
  return h(
    "div",
    { class: "detail-grid", onclick: (ev) => ev.stopPropagation() },
    h(
      "div",
      null,
      h("h4", null, "Request"),
      headerLines(spec.headers, ["if-match", "idempotency-key", "last-event-id"]),
      spec.bodyText ? jsonBlock(spec.bodyText) : h("div", { class: "dim" }, "(no body)"),
      h(
        "div",
        { class: "row", style: { marginTop: "8px" } },
        MUTATING.has(spec.method) && spec.headers["Idempotency-Key"] && h("button", { class: "btn small", onclick: replay(true) }, "↻ Replay (same Idempotency-Key)"),
        MUTATING.has(spec.method) && h("button", { class: "btn small", onclick: replay(false) }, "Resend with a new key"),
        spec.path !== "/api/v2/events/stream" && h("button", { class: "btn small ghost", onclick: curl }, "Copy as curl"),
      ),
    ),
    h("div", null, h("h4", null, `Response ${r.status}`), headerLines(r.headers, ["etag", "x-request-id", "www-authenticate"]), jsonBlock(r.body)),
  );
}

// ============================================================================ showcase

const PROPS = [
  {
    n: 1,
    title: "Identity is established by the server; rights are roles",
    v1: "The server mints its own HS256 tokens from secrets it holds; API keys, a single-user key and anonymous access; 25 scopes.",
    v2: "Only access tokens from the facility identity provider, verified with RS256 against its published keys. Four nested roles: Read ⊂ Edit ⊂ Run ⊂ Admin.",
    demos: [
      ["id-tokens", "Send forged and invalid tokens"],
      ["id-roles", "Roles bound every route"],
    ],
  },
  {
    n: 2,
    title: "A closed, versioned operation catalog — not a namespace",
    v1: "Clients name any plan the user group allows from the worker namespace; script upload and function execution are API routes.",
    v2: "A reviewed catalog of versioned operations, each with a JSON Schema and a device map; parameters are validated before anything is written. No plan, script, function or environment routes.",
    demos: [
      ["cat-surface", "List the whole API surface"],
      ["cat-reject", "Submit things outside the catalog"],
    ],
  },
  {
    n: 3,
    title: "Control is a lease on an identity; scheduling is separate",
    v1: "Anyone with the right scopes edits the queue at any time; the manager's optional lock uses a shared key, not an identity.",
    v2: "One principal holds a time-limited control lease; only the holder edits or runs. Admins may override, with a recorded reason. A started execution runs on without the lease.",
    demos: [
      ["lease-contend", "Contend for control"],
      ["lease-override", "Admin override, recorded"],
      ["lease-outlives", "Work outlives the lease"],
    ],
  },
  {
    n: 4,
    title: "Optimistic concurrency on the queue",
    v1: "Edits apply to whatever the queue is at that moment; two clients editing at once interleave silently.",
    v2: 'Every edit names the revision it was based on (If-Match: "qrev-N"). If the queue changed — another client or the scheduler — the edit is refused with 412 and nothing is written.',
    demos: [
      ["occ-race", "Two clients, one revision"],
      ["occ-headers", "Edits must name a revision"],
    ],
  },
  {
    n: 5,
    title: "Durable transactional state, append-only audit, push not poll",
    v1: "The queue lives in RE Manager's Redis; clients poll status; nothing records who did what.",
    v2: "SQLite is the authority. Every change is one transaction with an append-only event (actor, revision, payload), streamed over SSE; Last-Event-ID resumes without gaps.",
    demos: [
      ["push-resume", "Drop the stream, miss nothing"],
      ["push-audit", "Replay the audit log from event 0"],
    ],
  },
  {
    n: 6,
    title: "Fail closed when an outcome cannot be proven",
    v1: "After a hard worker death RE Manager kept reporting a running plan on a dead worker until the environment was destroyed or the manager restarted.",
    v2: "An attempt whose outcome cannot be proven becomes unknown, its execution is blocked and nothing more is dispatched. A proven failure or safe stop just ends the execution.",
    demos: [
      ["safe-stop", "Safe-stop a running operation"],
      ["fault-worker", "Kill RE Worker mid-operation", true],
    ],
  },
  {
    n: 7,
    title: "Explicit recovery; fenced worker authority",
    v1: "Recovery is ad hoc: destroy and reopen the environment; nothing records who checked what, or proves the worker had stopped.",
    v2: "A Run-role operator acknowledges with a note; accepted only while RE Manager proves nothing is running, and recorded as worker.fenced with that evidence. Only then is the environment reopened.",
    demos: [["recover", "Acknowledge recovery"]],
  },
  {
    n: 8,
    title: "Idempotent mutations; honest readiness",
    v1: "A POST retried after a timeout may queue the plan twice; health is inferred from /status.",
    v2: "Every mutation carries an Idempotency-Key: a retry returns the original response and nothing is repeated. /ready reports worker, environment, catalog and dispatch, and is 503 when any is not ready; a short RE Manager outage neither blocks nor loses the running operation.",
    demos: [
      ["idem-retry", "Retry one submission three times"],
      ["freeze-manager", "Freeze RE Manager mid-operation", true],
    ],
  },
];

const ARCH = `browser · agent
   │  HTTPS + SSE, bearer JWT from the identity provider
   ▼
bluesky-httpserver 2.x — the controller ◀──▶ SQLite (authority)
   │  OIDC · lease · catalog · queue · revision
   │  scheduler · attempts · events
   │  0MQ over ipc://, the only client RE Manager accepts
   ▼
RE Manager — relay, one operation at a time
   ▼
RE Worker ──▶ IOCs`;

function renderShowcase() {
  if (S.view !== "showcase") return;
  const scroll = $("#view").scrollTop;
  mount(
    $("#view"),
    h(
      "div",
      { class: "showcase-intro" },
      h(
        "div",
        { class: "card hero" },
        h("h1", null, "What ", h("span", null, "v2"), " guarantees, live"),
        h(
          "p",
          null,
          "v1 is a stateless front that forwards most routes to RE Manager. In v2 the HTTP server is the controller: it owns the queue and all durable control state, and RE Manager only relays one operation at a time to the worker. Each card below compares one property and runs it against this lab's controller.",
        ),
        h(
          "p",
          null,
          "Demos act as the lab personas — alice (Admin), bob (Run), carol (Edit), dave (Read) — and may take control from whoever holds it. Watch the event log on the right and the HTTP inspector below. Open a second window as someone else to see contention and push updates.",
        ),
      ),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Process and trust boundaries")), h("div", { class: "arch" }, ARCH)),
    ),
    h(
      "div",
      { class: "props" },
      PROPS.map((P) =>
        h(
          "div",
          { class: "card prop" },
          h("div", { class: "row" }, h("span", { class: "num" }, P.n), h("h3", null, P.title)),
          h("div", { class: "cmp" }, h("div", { class: "v1" }, h("b", null, "v1"), P.v1), h("div", { class: "v2" }, h("b", null, "v2"), P.v2)),
          h(
            "div",
            { class: "demos" },
            P.demos.map(([id, label, fault]) =>
              h(
                "button",
                { class: `btn small${fault ? " warn" : ""}`, disabled: S.demoRunning.has(id), onclick: () => runDemo(id, fault) },
                S.demoRunning.has(id) ? "⏳ " : "▶ ",
                label,
                fault ? h("span", { class: "role-tag" }, "lab fault") : null,
              ),
            ),
          ),
          P.demos.some((d) => d[2]) && h("div", { class: "lab-only" }, "Lab fault buttons signal this lab's RE Manager processes directly; the v2 API has no such route."),
          P.demos.map(([id]) =>
            h(
              "div",
              { class: "demo-log" },
              (S.demoLogs[id] || []).map((s) =>
                h("div", { class: `step ${s.cls}` }, h("span", { class: "mark" }, { pass: "✔", fail: "✘", note: "•", wait: "…" }[s.cls]), h("span", { class: "what" }, s.what, s.got ? h("span", { class: "got" }, `  → ${s.got}`) : null)),
              ),
              S.demoExtra[id] || null,
            ),
          ),
        ),
      ),
    ),
  );
  $("#view").scrollTop = scroll;
}

function demoLog(id) {
  S.demoLogs[id] = [];
  S.demoExtra[id] = null;
  const add = (cls, what, got) => {
    const step = { cls, what, got };
    S.demoLogs[id].push(step);
    invalidate("showcase");
    return step;
  };
  return {
    note: (what, got) => add("note", what, got),
    pass: (what, got) => add("pass", what, got),
    fail: (what, got) => add("fail", what, got),
    wait(what) {
      const step = add("wait", what);
      return {
        pass: (got) => Object.assign(step, { cls: "pass", got }) && invalidate("showcase"),
        fail: (got) => Object.assign(step, { cls: "fail", got }) && invalidate("showcase"),
      };
    },
    expect(what, r, status, code) {
      const ok = r.status === status && (!code || r.error?.code === code);
      add(ok ? "pass" : "fail", what, describe(r));
      return ok;
    },
  };
}

class DemoStop extends Error {}

async function runDemo(id, fault) {
  if (fault && !confirm("This signals a process of the lab's RE Manager (SIGKILL or SIGSTOP). Continue?")) return;
  const log = demoLog(id);
  S.demoRunning.add(id);
  invalidate("showcase");
  try {
    await DEMOS[id](log);
  } catch (e) {
    if (!(e instanceof DemoStop)) log.fail("demo error", e.message || String(e));
  } finally {
    S.demoRunning.delete(id);
    invalidate("showcase");
  }
}

const must = (ok, why) => {
  if (!ok) throw new DemoStop(why);
};
function refuse(log, cls, what, got) {
  log[cls](what, got);
  throw new DemoStop(what);
}

async function ensureControl(log, p = ALICE) {
  const r = await api("GET", "/api/v2/control-lease", { as: p, background: true });
  const lease = r.body?.lease;
  if (lease?.holder === p.sub) {
    const renew = await api("POST", "/api/v2/control-lease/renew", { as: p, body: { ttl_seconds: 600 }, label: "demo" });
    must(log.expect(`${p.sub} already has control; renews it`, renew, 200), "renew");
  } else if (!lease) {
    const take = await api("POST", "/api/v2/control-lease", { as: p, body: { ttl_seconds: 600 }, label: "demo" });
    must(log.expect(`${p.sub} takes control`, take, 201), "take control");
  } else {
    const o = await api("POST", "/api/v2/control-lease/override", {
      as: ALICE,
      body: { reason: `v1→v2 showcase demo needs control (was ${lease.holder})`, ttl_seconds: 600 },
      label: "demo",
    });
    must(log.expect(`alice (Admin) takes control from ${lease.holder}`, o, 200), "override");
    if (p.sub !== "alice") {
      await api("POST", "/api/v2/control-lease/release", { as: ALICE, label: "demo" });
      const take = await api("POST", "/api/v2/control-lease", { as: p, body: { ttl_seconds: 600 }, label: "demo" });
      must(log.expect(`${p.sub} takes control`, take, 201), "take control");
    }
  }
  scheduleLeaseRefresh();
}

async function readQueue(p = ALICE) {
  const r = await api("GET", "/api/v2/queue", { as: p, label: "demo" });
  return r;
}

const QUICK = { operation_id: "tst.count", operation_version: "1", parameters: { detectors: ["diag1"], num: 1 } };
const submit = (body, ifMatch, extra = {}) => api("POST", "/api/v2/operations", { as: ALICE, body, ifMatch, label: "demo", ...extra });

async function cleanupOperation(log, uid) {
  const q = await readQueue();
  if (!q.body?.operations?.some((o) => o.operation_uid === uid)) return log.note(`operation ${short(uid)} is no longer queued; nothing to clean up`);
  const c = await api("DELETE", `/api/v2/operations/${uid}`, { as: ALICE, ifMatch: q.etag, label: "demo cleanup" });
  log.note(`cleanup: cancel ${short(uid)}`, describe(c));
}

async function waitStep(log, what, pred, timeoutMs, after) {
  const step = log.wait(what);
  try {
    const ev = await waitForEvent(pred, timeoutMs, after);
    step.pass(`#${ev.event_id} ${ev.event_type}${summarize(ev) ? ` · ${summarize(ev)}` : ""}`);
    return ev;
  } catch (e) {
    step.fail(e.message);
    throw new DemoStop(what);
  }
}

async function pollReady(pred, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const res = await fetch("/ready");
    const body = await res.json().catch(() => ({}));
    if (pred(body, res.status)) return { status: res.status, body };
    if (Date.now() > deadline) throw new Error(`timed out; last /ready ${res.status} ${JSON.stringify(body)}`);
    await sleep(500);
  }
}

const readyText = ({ status, body }) => `${status} ${Object.entries(body).map(([k, v]) => `${k}: ${v}`).join(", ")}`;

const DEMOS = {
  async "id-tokens"(log) {
    const anon = await api("GET", "/api/v2/queue", { anon: true, label: "demo" });
    log.expect("GET /queue with no Authorization header", anon, 401, "unauthenticated");
    log.note("WWW-Authenticate", anon.headers["www-authenticate"]);
    for (const [kind, what] of Object.entries(S.info.bad_tokens)) {
      const token = await badToken(kind, ALICE);
      const r = await api("GET", "/api/v2/queue", { token, tokenLabel: kind, label: "demo" });
      log.expect(`${what} (sub=alice, QueueServer.Admin)`, r, 401, "unauthenticated");
    }
    const ok = await api("GET", "/api/v2/queue", { as: ALICE, label: "demo" });
    log.expect("The identity provider's real token for alice", ok, 200);
  },

  async "id-roles"(log) {
    log.expect("dave (Read) reads the queue", await api("GET", "/api/v2/queue", { as: DAVE, label: "demo" }), 200);
    const d = await api("POST", "/api/v2/control-lease", { as: DAVE, body: { ttl_seconds: 60 }, label: "demo" });
    log.expect("dave (Read) tries to take control", d, 403, "insufficient_role");
    const q = await readQueue();
    const c = await api("POST", "/api/v2/queue-executions", { as: CAROL, body: {}, ifMatch: q.etag, label: "demo" });
    log.expect("carol (Edit) tries to start the queue", c, 403, "insufficient_role");
    const b = await api("POST", "/api/v2/control-lease/override", { as: BOB, body: { reason: "I want it", ttl_seconds: 60 }, label: "demo" });
    log.expect("bob (Run) tries to override the lease", b, 403, "insufficient_role");
    const forged = await api("POST", "/api/v2/operations", {
      as: ALICE,
      body: { ...QUICK, submitted_by: "bob" },
      ifMatch: q.etag,
      label: "demo",
    });
    log.expect("alice submits a body that names another actor (submitted_by: bob)", forged, 422, "invalid_request");
    log.note("Who did what is always the verified token's principal; the body cannot say otherwise.");
  },

  async "cat-surface"(log) {
    const r = await api("GET", "/api/v2/openapi.json", { as: DAVE, label: "demo" });
    if (!log.expect("GET /api/v2/openapi.json (needs a token: even the schema is not anonymous)", r, 200)) return;
    const routes = Object.entries(r.body.paths).flatMap(([path, methods]) => Object.keys(methods).map((m) => [m.toUpperCase(), path, methods[m].operationId]));
    const banned = /plan|script|function|environment|permission|zmq/i;
    const bad = routes.filter(([, path]) => banned.test(path));
    if (bad.length) log.fail("routes that forward raw RE Manager concepts", bad.map((b) => b[1]).join(", "));
    else log.pass(`${routes.length} routes; none contains plan, script, function, environment, permissions or zmq`);
    S.demoExtra["cat-surface"] = h("div", { class: "paths" }, routes.map(([m, path, op]) => h("div", null, h("span", { class: `method m-${m}` }, m.padEnd(6)), " ", path, h("span", { class: "dim" }, `  ${op}`))));
    invalidate("showcase");
  },

  async "cat-reject"(log) {
    await ensureControl(log);
    const q0 = await readQueue();
    log.note(`queue revision before: ${q0.etag}`);
    const tries = [
      ["A raw plan name (count) instead of a catalog operation", { operation_id: "count", operation_version: "1", parameters: { detectors: ["diag1"] } }, "unknown_operation"],
      ["A version the catalog does not offer (tst.count v2)", { ...QUICK, operation_version: "2" }, "unknown_operation"],
      ["num: 0 and an undeclared exposure parameter", { ...QUICK, parameters: { detectors: ["diag1"], num: 0, exposure: 1 } }, "invalid_parameters"],
      ["A device the operation's device map does not offer (dcm_c1)", { ...QUICK, parameters: { detectors: ["dcm_c1"] } }, "invalid_parameters"],
      ["Energy outside the operation's range (12 keV)", { operation_id: "bmm.change_energy", operation_version: "1", parameters: { energy: 12000, crystal: "si111" } }, "invalid_parameters"],
    ];
    for (const [what, body, code] of tries) {
      const r = await submit(body, q0.etag);
      log.expect(what, r, 422, code);
      for (const e of r.error?.details?.errors || []) log.note(`  ${e.path}`, e.message);
    }
    const q1 = await readQueue();
    if (q1.etag === q0.etag) log.pass("Nothing was written: the queue revision did not move", q1.etag);
    else log.fail("queue revision moved (another client may have edited meanwhile)", `${q0.etag} → ${q1.etag}`);
  },

  async "lease-contend"(log) {
    await ensureControl(log);
    const q = await readQueue(BOB);
    const r = await api("POST", "/api/v2/operations", { as: BOB, body: QUICK, ifMatch: q.etag, label: "demo" });
    log.expect("bob (Run) submits while alice holds control", r, 403, "lease_required");
    const t = await api("POST", "/api/v2/control-lease", { as: BOB, body: { ttl_seconds: 60 }, label: "demo" });
    log.expect("bob tries to take control", t, 409, "lease_held");
    if (t.error?.details) log.note("details", JSON.stringify(t.error.details));
    const a = await api("POST", "/api/v2/control-lease", { as: ALICE, body: { ttl_seconds: 60 }, label: "demo" });
    log.expect("alice tries to take control she already holds (the holder renews instead)", a, 409, "lease_held");
  },

  async "lease-override"(log) {
    await ensureControl(log);
    log.expect("alice releases control", await api("POST", "/api/v2/control-lease/release", { as: ALICE, label: "demo" }), 200);
    log.expect("bob takes control", await api("POST", "/api/v2/control-lease", { as: BOB, body: { ttl_seconds: 600 }, label: "demo" }), 201);
    const after = S.cursor;
    const reason = "Shift change: taking over from bob";
    const o = await api("POST", "/api/v2/control-lease/override", { as: ALICE, body: { reason, ttl_seconds: 600 }, label: "demo" });
    log.expect("alice (Admin) overrides, giving a reason", o, 200);
    await waitStep(log, "lease.overridden is in the audit log with the previous holder and the reason", (e) => e.event_type === "lease.overridden", 10000, after);
    const blank = await api("POST", "/api/v2/control-lease/override", { as: ALICE, body: { reason: "   ", ttl_seconds: 600 }, label: "demo" });
    log.expect("An override without a reason is refused", blank, 422, "invalid_request");
    scheduleLeaseRefresh();
  },

  async "lease-outlives"(log) {
    if (activeExecution()) refuse(log, "note", "A queue execution is already active; let it finish first.");
    await ensureControl(log);
    const q = await readQueue();
    const after = S.cursor;
    const s = await submit({ ...QUICK, parameters: { detectors: ["diag1"], num: 3, delay: 1 } }, q.etag);
    must(log.expect("alice queues a 3-second count", s, 201), "submit");
    const st = await api("POST", "/api/v2/queue-executions", { as: ALICE, body: {}, ifMatch: s.etag, label: "demo" });
    must(log.expect("alice starts the queue", st, 201), "start");
    log.expect("alice releases control right away", await api("POST", "/api/v2/control-lease/release", { as: ALICE, label: "demo" }), 200);
    await waitStep(log, "the admitted operation still succeeds with nobody in control", (e) => e.event_type === "operation.succeeded" && e.operation_uid === s.body.operation_uid, 120000, after);
    await waitStep(log, "the execution completes", (e) => e.queue_execution_uid === st.body.queue_execution_uid && /completed|stopped/.test(e.event_type), 120000, after);
    scheduleLeaseRefresh();
  },

  async "occ-race"(log) {
    await ensureControl(log);
    const q = await readQueue();
    log.note(`both clients read the queue at ${q.etag}`);
    const [a, b] = await Promise.all([
      submit({ ...QUICK, parameters: { detectors: ["diag1"] } }, q.etag, { label: "client A" }),
      submit({ ...QUICK, parameters: { detectors: ["diag2"] } }, q.etag, { label: "client B" }),
    ]);
    const won = [a, b].filter((r) => r.status === 201);
    const lost = [a, b].filter((r) => r.status === 412);
    log.note("client A", describe(a));
    log.note("client B", describe(b));
    if (won.length === 1 && lost.length === 1) {
      log.pass("Exactly one edit landed; the other was refused and wrote nothing", `412 ETag ${lost[0].etag}, details ${JSON.stringify(lost[0].error.details)}`);
      await cleanupOperation(log, won[0].body.operation_uid);
    } else {
      log.fail("expected one 201 and one 412", `${a.status}, ${b.status}`);
      for (const r of won) await cleanupOperation(log, r.body.operation_uid);
    }
  },

  async "occ-headers"(log) {
    await ensureControl(log);
    const q = await readQueue();
    log.expect("No If-Match", await submit(QUICK, undefined), 428, "precondition_required");
    log.expect('If-Match: * (would bypass the check)', await submit(QUICK, "*"), 400, "invalid_if_match");
    log.expect(`A weak tag W/${q.etag}`, await submit(QUICK, `W/${q.etag}`), 400, "invalid_if_match");
    log.expect("No Idempotency-Key", await submit(QUICK, q.etag, { key: null }), 400, "idempotency_key_required");
    if (q.body.revision > 0) {
      const old = await submit(QUICK, '"qrev-0"');
      log.expect('An old revision, If-Match: "qrev-0"', old, 412, "stale_revision");
      log.note("the 412 carries the current revision", old.etag);
    }
  },

  async "push-resume"(log) {
    await ensureControl(log);
    const before = S.cursor;
    const gaps = S.gaps;
    pauseStream();
    log.note(`stream dropped at cursor #${before}`);
    for (let i = 0; i < 3; i++) await api("POST", "/api/v2/control-lease/renew", { as: ALICE, body: { ttl_seconds: 600 }, label: "while disconnected" });
    log.note("3 lease renewals recorded while this page was not listening");
    await sleep(800);
    log.note(`reconnecting with Last-Event-ID: ${S.cursor}`);
    resumeStream();
    await waitStep(log, "events recorded while disconnected arrive after reconnecting", (e) => e.event_type === "lease.renewed" && e.event_id > before + 2, 15000, before);
    if (S.gaps === gaps) log.pass("event ids are consecutive: nothing was missed or repeated", `cursor #${S.cursor}`);
    else log.fail("gap detected", `${S.gaps - gaps} gaps`);
  },

  async "push-audit"(log) {
    let after = 0;
    const all = [];
    for (;;) {
      const r = await api("GET", `/api/v2/events?after=${after}&limit=1000`, { as: DAVE, label: "demo" });
      must(r.ok, describe(r));
      if (!r.body.events.length) break;
      all.push(...r.body.events);
      after = all[all.length - 1].event_id;
    }
    log.pass(`GET /events from 0 returned ${all.length} events (paged by 1000)`);
    const contiguous = all.every((e, i) => e.event_id === i + 1);
    contiguous ? log.pass("event ids run 1…N with no gaps") : log.fail("event ids are not contiguous");
    const monotonic = all.every((e, i) => i === 0 || e.queue_revision >= all[i - 1].queue_revision);
    monotonic ? log.pass("the queue revision never goes backwards") : log.fail("revision went backwards");
    const streamed = S.events.filter((e) => e.event_id <= after);
    const same = streamed.length === all.length && streamed.every((e, i) => e.event_id === all[i].event_id && e.event_type === all[i].event_type);
    same ? log.pass("the SSE stream delivered exactly the same log to this page") : log.note("stream and log differ (page opened mid-way?)", `${streamed.length} vs ${all.length}`);
    const actors = {};
    for (const e of all) actors[e.actor_kind === "principal" ? e.actor_id : e.actor_kind] = (actors[e.actor_kind === "principal" ? e.actor_id : e.actor_kind] || 0) + 1;
    log.note("events by actor", Object.entries(actors).map(([k, v]) => `${k} ${v}`).join(", "));
    log.note("Stop and restart the controller, then reload this page: all of it is still here.");
  },

  async "safe-stop"(log) {
    if (activeExecution()) refuse(log, "note", "A queue execution is already active; let it finish first.");
    await ensureControl(log);
    const q = await readQueue();
    const after = S.cursor;
    const s = await submit({ ...QUICK, parameters: { detectors: ["diag1"], num: 30, delay: 1 } }, q.etag);
    must(log.expect("alice queues a 30-second count", s, 201), "submit");
    const st = await api("POST", "/api/v2/queue-executions", { as: ALICE, body: {}, ifMatch: s.etag, label: "demo" });
    must(log.expect("alice starts the queue", st, 201), "start");
    const running = await waitStep(log, "the operation is running", (e) => e.event_type === "operation.running" && e.operation_uid === s.body.operation_uid, 120000, after);
    await sleep(2500);
    const stop = await api("POST", `/api/v2/attempts/${running.attempt_uid}/safe-stop`, { as: ALICE, label: "demo" });
    log.expect("alice requests a safe stop (no If-Match: an emergency stop never fails on a stale revision)", stop, 202);
    await waitStep(log, "RE Worker pauses at a checkpoint and aborts: a proven outcome", (e) => e.event_type === "operation.aborted" && e.operation_uid === s.body.operation_uid, 60000, after);
    await waitStep(log, "the execution stops — no block, nothing to acknowledge", (e) => e.event_type === "queue_execution.stopped" && e.queue_execution_uid === st.body.queue_execution_uid, 30000, after);
  },

  async "fault-worker"(log) {
    const ready = await pollReady(() => true, 0).catch(() => null);
    if (!ready?.body.ready) refuse(log, "fail", "the controller must be ready first", ready ? readyText(ready) : "no answer");
    if (activeExecution()) refuse(log, "note", "A queue execution is already active; let it finish first.");
    await ensureControl(log);
    const q = await readQueue();
    const after = S.cursor;
    const s = await submit({ ...QUICK, parameters: { detectors: ["xas_det"], num: 60, delay: 1 } }, q.etag);
    must(log.expect("alice queues a 60-second count", s, 201), "submit");
    const st = await api("POST", "/api/v2/queue-executions", { as: ALICE, body: {}, ifMatch: s.etag, label: "demo" });
    must(log.expect("alice starts the queue", st, 201), "start");
    await waitStep(log, "the operation is running", (e) => e.event_type === "operation.running" && e.operation_uid === s.body.operation_uid, 120000, after);
    await sleep(2000);
    const procs = await (await fetch("/lab/processes")).json();
    log.note("RE Manager processes", `watchdog ${procs.watchdog}, manager ${procs.manager}, worker ${procs.worker}`);
    const kill = await fetch("/lab/faults/kill-worker", { method: "POST" });
    const kj = await kill.json();
    if (!kill.ok) refuse(log, "fail", "lab: SIGKILL RE Worker", kj.error);
    log.pass("lab: SIGKILL RE Worker", `pid ${kj.pid}`);
    await waitStep(log, "the controller cannot prove how it ended: attempt unknown", (e) => e.event_type === "operation.unknown" && e.operation_uid === s.body.operation_uid, 90000, after);
    await waitStep(log, "the execution is blocked", (e) => e.event_type === "queue_execution.blocked", 30000, after);
    const r = await pollReady((b) => b.dispatch === "blocked", 10000);
    log.pass("GET /ready", readyText(r));
    const q2 = await readQueue();
    const again = await api("POST", "/api/v2/queue-executions", { as: ALICE, body: {}, ifMatch: q2.etag, label: "demo" });
    log.expect("starting the queue again is refused", again, 409, "dispatch_blocked");
    log.note("Nothing runs until recovery is acknowledged — property 7.");
  },

  async recover(log) {
    const r0 = await pollReady(() => true, 0).catch(() => null);
    if (!S.block && r0?.body.dispatch !== "blocked") refuse(log, "note", "Nothing is blocked. Run “Kill RE Worker mid-operation” (property 6) first.");
    await ensureControl(log);
    const q = await readQueue();
    const c = await api("POST", "/api/v2/recovery/acknowledge", { as: CAROL, body: { note: "looks fine" }, ifMatch: q.etag, label: "demo" });
    log.expect("carol (Edit) tries to acknowledge", c, 403, "insufficient_role");
    const blank = await api("POST", "/api/v2/recovery/acknowledge", { as: ALICE, body: { note: "  " }, ifMatch: q.etag, label: "demo" });
    log.expect("alice acknowledges without saying what she checked", blank, 422, "invalid_request");
    const after = S.cursor;
    const note = "Checked: RE Worker process is gone, motors at rest, sample safe.";
    const step = log.wait("alice acknowledges with a note, retrying while RE Manager does not prove idle");
    let ack;
    for (let i = 0; i < 60; i++) {
      const qq = await readQueue();
      ack = await api("POST", "/api/v2/recovery/acknowledge", { as: ALICE, body: { note }, ifMatch: qq.etag, label: "demo" });
      if (ack.error?.code !== "no_fence_evidence") break;
      await sleep(1000);
    }
    ack.ok ? step.pass(describe(ack) + ` · execution ${ack.body.state}`) : step.fail(describe(ack));
    must(ack.ok, "ack");
    const fenced = await waitStep(log, "worker.fenced records the note and the evidence", (e) => e.event_type === "worker.fenced", 10000, after);
    log.note("evidence", JSON.stringify(fenced.payload.status));
    const step2 = log.wait("the controller reopens the worker environment; /ready returns to 200");
    try {
      step2.pass(readyText(await pollReady((b) => b.ready, 180000)));
    } catch (e) {
      step2.fail(e.message);
    }
    log.note("Operations that were queued stay queued; start a new execution to continue.");
  },

  async "idem-retry"(log) {
    await ensureControl(log);
    const q = await readQueue();
    const key = uuid();
    log.note(`Idempotency-Key ${key}, If-Match ${q.etag}`);
    const body = { ...QUICK, parameters: { detectors: ["nano_bpm"], num: 1 } };
    const rs = [];
    for (let i = 0; i < 3; i++) rs.push(await submit(body, q.etag, { key, label: `attempt ${i + 1}` }));
    rs.forEach((r, i) => log.note(`send ${i + 1}`, `${describe(r)} · operation ${short(r.body?.operation_uid)}`));
    const uids = new Set(rs.map((r) => r.body?.operation_uid));
    if (rs.every((r) => r.status === 201) && uids.size === 1) log.pass("all three got the same 201 and the same operation", short([...uids][0]));
    else log.fail("expected one operation", [...uids].join(", "));
    const q2 = await readQueue();
    const count = q2.body.operations.filter((o) => o.operation_uid === [...uids][0]).length;
    log[count === 1 ? "pass" : "fail"]("the queue holds it once", `${count}`);
    const conflict = await submit({ ...body, parameters: { detectors: ["xrd_det"] } }, q.etag, { key, label: "same key, new body" });
    log.expect("the same key with a different body", conflict, 409, "idempotency_conflict");
    await cleanupOperation(log, [...uids][0]);
  },

  async "freeze-manager"(log) {
    if (activeExecution()) refuse(log, "note", "A queue execution is already active; let it finish first.");
    await ensureControl(log);
    const q = await readQueue();
    const after = S.cursor;
    const s = await submit({ ...QUICK, parameters: { detectors: ["diag1"], num: 25, delay: 1 } }, q.etag);
    must(log.expect("alice queues a 25-second count", s, 201), "submit");
    const st = await api("POST", "/api/v2/queue-executions", { as: ALICE, body: {}, ifMatch: s.etag, label: "demo" });
    must(log.expect("alice starts the queue", st, 201), "start");
    await waitStep(log, "the operation is running", (e) => e.event_type === "operation.running" && e.operation_uid === s.body.operation_uid, 120000, after);
    const freeze = await fetch("/lab/faults/freeze-manager", { method: "POST" });
    const fj = await freeze.json();
    if (!freeze.ok) refuse(log, "fail", "lab: SIGSTOP RE Manager", fj.error);
    log.pass(`lab: SIGSTOP RE Manager and its watchdog for ${fj.seconds} s; RE Worker keeps running the plan`, `pids ${fj.frozen.join(", ")}`);
    const down = log.wait("/ready says RE Manager is unreachable — coarse fields only, no addresses or PIDs");
    try {
      down.pass(readyText(await pollReady((b, status) => status === 503 && b.worker === "unreachable", 30000)));
    } catch (e) {
      down.fail(e.message);
    }
    const op = S.ops.get(s.body.operation_uid);
    log[op?.state === "running" ? "pass" : "fail"](
      "the attempt stays running: silence shorter than the 60 s grace window proves nothing either way",
      op?.state,
    );
    const up = log.wait("RE Manager answers again and the controller reconnects");
    try {
      up.pass(readyText(await pollReady((b) => b.ready, 120000)));
    } catch (e) {
      up.fail(e.message);
    }
    const outcome = await waitStep(log, "the operation ends", (e) => e.operation_uid === s.body.operation_uid && /succeeded|failed|aborted|unknown/.test(e.event_type), 120000, after);
    log[outcome.event_type === "operation.succeeded" ? "pass" : "fail"]("its outcome is proven from RE Manager's history after reconnecting: no block", outcome.event_type);
    const ended = await waitStep(log, "the execution ends", (e) => e.queue_execution_uid === st.body.queue_execution_uid && /completed|stopped|blocked/.test(e.event_type), 60000, after);
    log[ended.event_type === "queue_execution.completed" ? "pass" : "fail"]("the execution completed normally", ended.event_type);
  },
};

// ============================================================================ timers

function tick() {
  const now = Date.now();
  for (const el of document.querySelectorAll("[data-countdown]")) {
    const left = Number(el.dataset.countdown) - now;
    el.textContent = left > 0 ? ` ${fmtDur(left)}` : " expired";
  }
  for (const el of document.querySelectorAll("[data-elapsed]")) el.textContent = fmtDur(now - Number(el.dataset.elapsed));
  for (const el of document.querySelectorAll("[data-ago]")) el.textContent = `${fmtDur(now - Number(el.dataset.ago))} ago`;
}

async function leaseKeeper() {
  const L = S.lease;
  if (!L) return;
  const left = new Date(L.expires_at).getTime() - Date.now();
  if (left <= 0) return refreshLease();
  if (isMine(L) && S.autoRenew && !S.renewing && left < Math.max(5000, (S.leaseTtl * 1000) / 3)) {
    S.renewing = true;
    try {
      await renewLease(true);
    } finally {
      S.renewing = false;
    }
  }
}

// ============================================================================ boot

const RENDER = {
  header: renderHeader,
  view: renderView,
  catalog: renderCatalog,
  queue: renderQueue,
  exec: renderExec,
  history: renderHistory,
  showcase: renderShowcase,
  evhead: renderEvHead,
  inspector: renderInspector,
  drawer: () => S.drawer && renderDrawer(),
};

async function boot() {
  S.info = await (await fetch("/lab/info")).json();
  document.addEventListener("click", () => {
    if (S.menuOpen) {
      S.menuOpen = false;
      invalidate("header");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.drawer) closeDrawer();
  });
  buildEventsPanel();
  invalidate("header", "view", "inspector");
  refreshReady();
  refreshCatalog();
  refreshLease();
  refreshQueue();
  streamLoop();
  setInterval(tick, 1000);
  setInterval(refreshReady, 2000);
  setInterval(pollAttempt, 2000);
  setInterval(leaseKeeper, 1000);
  setInterval(() => (S.catalog ? refreshCatalog() : null), 30000);
  setInterval(() => !S.catalog && refreshCatalog(), 3000);
}

boot();
