# bluesky-httpserver V2: remaining gaps

What still stands between the v2 controller and the service it is meant to be: one that remote users and AI
agents can safely submit Bluesky plans to. Replacing local, flexible bluesky-queueserver deployments is not a
goal, so gaps that matter only there are left out. Section letters in parentheses (A1, B3, …) refer to
`V2_FEATURE_REQUESTS.md`.

Reviewed on 2026-11-07: this repo at `fda8cb0` (branch `v2-init`), and RE Manager from
`thopkins32/bluesky-queueserver`, branch `v2`, at `49af55a`. Line numbers refer to those commits. **HS** is
`src/bluesky_httpserver/v2/` and **QS** is the fork's `src/bluesky_queueserver/manager/`. *Reproduced* means
shown by running code, against the test fixtures (fake relay) or a real RE Manager with Redis; *inferred* means
read from the code and not run.

**Status, later the same day.** A first pass fixed G1, G2 and G4 (on the fork), G6 and G16, and parts of G3, G7,
G8 and G9; each of those sections ends with what changed. All changes are uncommitted, the fork's in
`./bluesky-queueserver`, and line numbers in the findings still refer to the reviewed commits. The v2 suite now
has 255 tests and passes against the fixed fork with Redis running, including the three in
`tests/test_re_manager.py` that use a real RE Manager; pyright and ruff are clean.

## What already holds

The store, the queue revision and idempotency model, the gateway's fail-closed state machine, token checking
and the route role gates all match the spec. The v2 suite passes against the fork with Redis running: 233 tests,
including the two in `tests/test_re_manager.py` that use a real RE Manager. pyright reports no errors; ruff
reports three import-order issues in `v2-lab/` (`demo.py`, `myscan.py`, `stress.py`).

The gaps sit mostly in what that core trusts: the worker (G1, G2), RE Manager's socket (G3), Redis (G4), the
restore path (G5) and the client agents will use (G6).

## Must fix before remote exposure

Each of these breaks a guarantee the design doc makes.

### G1. The worker turns strings into devices and plans outside the device map (A1)

For a parameter with no annotation, `prepare_plan` turns on plan-name and device-name conversion
(QS `profile_ops.py:963-965`), recursing into lists and dict values (`profile_ops.py:922-947`). `@operation`
sets no conversion policy (QS `annotation_decorator.py:401-423`), and the controller checks and substitutes only
the parameters in the device map (HS `catalog.py`). So a string that names any device, subdevice or plan allowed
to the user group reaches the plan as that object. The design accepts enforcing the catalog at the door only
("What this accepts"), but that assumes the worker runs what the door checked.

- Reproduced: a catalog built the way `zmq_relay.py` builds it, parameters accepted by `Catalog.validate`, then
  `prepare_plan`. `sample="motor1"` became a `SynAxis`; in `md={"note": "motor2.velocity", "x": ["det2", "count"]}`
  the note became a `Signal`, and the list the det2 device and the `count` plan function. A parameter hinted
  `label: str` stayed a string.
- Same cause, opposite effect: a device-mapped parameter with an ordinary hint (`list`, `list[Any]`, `Any`)
  reaches the plan as the substituted string, so the plan fails at run time. Protocol hints such as
  `Sequence[Readable]` and `Movable` convert correctly (reproduced; the policy comes from the hints,
  QS `profile_ops.py:3166-3196`).
- The lab is safe only because `v2-lab/profile/10-operations.py` also applies `@parameter_annotation_decorator`.

**Fix.** For any plan carrying an operation descriptor, turn plan and device conversion off for every parameter,
and give each device-mapped parameter an annotation whose `devices` are exactly the map's values. The worker then
enforces the map too, which the design lists under "Added later".

**Status.** Fixed on the fork. For a plan with an operation descriptor, `_process_plan` turns off plan-name
conversion and expression evaluation for every parameter, and device-name conversion outside the device map. Each
device-mapped parameter gets an annotation whose devices are exactly the map's values, so a `list` or `Any` hint
converts and a name outside the map is refused by the worker. `operation()` refuses map values that are not device
or subdevice names. With `@parameter_annotation_decorator` also applied, in either order, the map's annotation
replaces it for device-mapped parameters, keeping its description and default; other parameters keep theirs for
validation but convert nothing, so a `plans` or `devices` enum or a `__CALLABLE__` parameter of an operation plan
now arrives as a string, for v1 clients too. After the fix the reproduction leaves `sample`, `md.note` and the
nested list as strings, and `list` and `list[Any]` parameters convert. On a copy of the lab, with plans that fail
if a device arrives as a string, the three lab operations succeed through the controller.

### G2. A dying worker can make the fence evidence false (A2)

The dead-worker branch of the poll loop (QS `manager.py:823-830`) also runs while the environment is being
created (`:769`). It calls `_kill_re_worker` (`:673-678`), which completes `_fut_manager_task_completed` with
`False`; `_start_re_worker_task` then gives up and sets `IDLE` (`:567, 587`) while the kill task is still
running. That task spends up to 10 s asking whether the watchdog's *current* worker is alive, then sets
`_environment_exists=False` and `IDLE` and records any running item as failed (`:696-713`).

In that window the gateway sees idle with no environment and opens one (HS `gateway.py:332, 479-484`). The
leftover kill task then records worker #2's plan as dead and reports idle with no environment while worker #2
keeps running. `fence_evidence()` (HS `gateway.py:210`) accepts that status, so acknowledging recovery opens
worker #3 next to it. The design's fence is "the manager reports idle", which is adequate only with a correct A2.

- Reproduced against a real RE Manager: the second `environment_open` was accepted at 6.07 s; at 15.62 s the
  manager reported idle with no environment while the worker process was alive. A plan dispatched at 7.12 s was
  recorded `failed: RE Worker process terminated unexpectedly` at 15.58 s, then finished on the orphaned worker
  at 19.19 s.
- Inferred, same cause: `environment_destroy` on a hung worker can start a second kill task.

**Fix.** While the environment is being created, the dead-worker path only completes the future and leaves
cleanup to `_start_re_worker_task`. `_start_re_worker_task` and `_stop_re_worker_task` leave the state alone once
it is `DESTROYING_ENVIRONMENT`. Drop the `is_alive` loop from `_kill_re_worker_task`: the watchdog's
`kill_re_worker` already kills and joins the process.

**Status.** Fixed on the fork as proposed. Also, `_kill_re_worker` refuses a second destroy while one runs, and if
the watchdog doesn't confirm a kill within 10 s the manager keeps reporting the environment, so nothing opens a
worker next to it. Rerun: idle with no environment at 6.08 s, the second worker opened at 7.11 s and stayed, a
third `environment_open` was refused ("environment already exists"), and the dispatched plan completed. A worker
killed after the environment is open still leaves the manager idle with no environment and the item `failed` with
the diagnostic. Regression test: `test_zmq_api_environment_destroy_05_worker_killed_while_opening`.

### G3. Controller-fronted mode leaves RE Manager's socket surface open (A3)

- **A deny list, not the kept list** (QS `manager.py:63-85`). Of the 50 methods, 19 are kept, 19 disabled and 12
  in neither list. `manager_stop`, `queue_stop_cancel` and `kernel_interrupt` succeed (reproduced), and
  `manager_kill` and `manager_test` are not blocked either (inferred). `manager_stop safe_off` shuts RE Manager
  down even mid-plan, and the process exits with status 0, so systemd's `Restart=on-failure` doesn't bring it
  back (reproduced). Also reachable, read-only: `queue_get`, `queue_item_get`, `permissions_get`, `task_status`,
  `task_result`, `lock_info`, `re_metadata`.
- **IPython kernel.** With `--use-ipython-kernel=ON` the kernel's TCP ports stay open (on the LAN with
  `--ipython-kernel-ip=auto`), `config_get` returns `ip_connect_info` including the HMAC key
  (QS `manager.py:1966-1973`), and the worker logs the key at INFO (QS `worker.py:1931-1933`). Anyone who can read
  the logs and reach the host can run code as the service user (reproduced).
- **`ipc://` is checked by prefix only**, on both sides (QS `start_manager.py:705`; HS `settings.py:117-122`).
  `ipc://@name` is a Linux abstract socket, which has no file permissions; the manager starts on one in
  controller-fronted mode and any local user can connect (reproduced). A path in a shared directory can be
  pre-created by another user (inferred). Socket files get the process umask.
- **A saved lock can't be released.** A lock saved in Redis before the switch is restored at startup
  (QS `manager.py:1547-1552`) and `unlock` is disabled, so `environment_open`, `queue_item_execute`, `re_pause`
  and `re_abort` are all refused: no dispatch, no safe stop, and `/ready` doesn't say why (reproduced).
- **The socket is the whole boundary.** By design, anything that reaches it can `queue_item_execute` any plan
  allowed to *any* user group, with arbitrary arguments, metadata and `user`; strings become that group's devices
  and plans, and `__CALLABLE__` parameters are `eval`ed (QS `profile_ops.py:915-919`, inferred). Reproduced:
  `mv(motor1, 5)` as user `someone-else` in group `root` completed and was recorded under that user.

**Fix.** Gate on an allowlist equal to A3's kept list, plus any read-only methods chosen on purpose. Refuse
controller-fronted mode with the IPython kernel on (at least with a non-loopback kernel IP); return an empty
`ip_connect_info` and stop logging the key. Require absolute `ipc:///` paths on both sides, and check at startup
that the socket directory belongs to the service user and is not group- or world-writable (or set umask 077
before binding). Clear or refuse a saved lock at startup, and show a lock in readiness.

**Status.** Partly fixed. Controller-fronted mode now allows exactly the 19 kept methods; any other, `manager_stop`
included, gets "disabled in controller-fronted mode" (checked on the lab copy for `manager_stop`,
`queue_stop_cancel`, `queue_get` and `permissions_get`), so a controller-fronted manager is stopped with SIGTERM.
RE Manager refuses to start with the IPython kernel on, or with an `ipc://` address that is not an absolute path,
and the controller's settings refuse the same addresses. Still open: the socket directory's owner and mode are not
checked, the saved lock, and whatever reaches the socket can still run any plan allowed to any user group.

### G4. Redis holds the outcome evidence but sits outside the boundary (A3, D2)

The gateway trusts a history entry above every other signal (HS `gateway.py:404-411`; later entries win,
`:376-378`), and RE Manager keeps its history in Redis. httpserver never writes Redis ("What this accepts"), but
it believes what is there.

- The lab runs Redis on TCP with no authentication (`v2-lab/pixi.toml:55`). Any local account can write a
  `completed` entry for a running attempt or resolve an `unknown` one (inferred). That defeats fail-closed, and
  A3's "who may connect is a filesystem question" no longer holds.
- A unix socket or a password is blocked by a queueserver bug: `PlanQueueOperations.start` never assigns `host`
  when the address contains `://` (QS `plan_queue_ops.py:264-267`), so `unix:///…` and `redis://:secret@…` fail
  with an `OSError` caused by an `UnboundLocalError` (reproduced). Inherited from upstream, not part of A1-A3.
- The lab runs Redis without persistence (`--save '' --appendonly no`), so a Redis restart loses the evidence.
  That fails safe (attempts stay `unknown`), but D2 must choose.

**Fix.** The one-line `host` fix in queueserver. D2 runs Redis on a unix socket inside the `0700` directory as
the service user, or with an ACL whose secret is not on the command line, and decides on persistence.

**Status.** The `host` bug is fixed on the fork: an address containing `://` is passed through unchanged. A
controller-fronted manager started with `--redis-addr='unix:///…/redis.sock?password=…'`, against a Redis that
listens only on that socket and requires a password, wrote its keys and answered `history_get`. Still open for
D2: the lab runs Redis on TCP without authentication or persistence, and RE Manager logs the Redis address,
password included, at startup and in connection errors, so a socket in the `0700` directory beats a password.

### G5. Restoring a backup re-runs work (B6)

`restore` (HS `store/admin.py:88`) copies the backup over the store; nothing accounts for what happened after the
backup was taken.

- Reproduced: a backup taken while an execution was running, two operations finished, then restore and restart.
  The gateway sent operation #1 to the worker again. The highest event id went back from 11 to 7 and the queue
  revision from 10 to 6, so both are reused.
- A client resuming with `Last-Event-ID` above the restored tail never sees the new events (reproduced: the
  restored log's events 3 and 4 never arrived). An old ETag can match a different queue.

**Fix.** Restore marks running executions `blocked` and live attempts `unknown`, so recovery goes through
acknowledgement. Add a store epoch, changed by restore, to ETags and event ids; at least reject a `Last-Event-ID`
beyond the newest event with a distinct error.

### G6. The client undoes If-Match and idempotency (C1)

- **If-Match.** `_mutate` sends one shared `self._revision` (HS `client/async_client.py:149-154`). Every
  response's ETag overwrites it (`:129-131`); every event seen through `events()` or `stream_events()` raises it
  (`:134-137`, from `:278` and `:872`); and when nothing has been seen it fetches the current revision
  (`:152-153`), so a first edit can never be stale and 428 never happens. Reproduced: an agent read at revision 2,
  the scheduler claimed, and the client sent revision 5; the edit was accepted where it should have got 412.
  Tasks or threads sharing a client, which the docs allow (`:52`; `sync_client.py:74`), have the same problem,
  and `edit_queue` never re-decides on a stale queue (`:727-728`).
- **Idempotency.** A new `Idempotency-Key` is made per call and never exposed (`:149`). The server's fingerprint
  includes `If-Match` (HS `api/preconditions.py:146`), so a same-key retry from a new client gets 409, and
  following the 409's advice ("a new request needs a new key", `api/errors.py:66-67`) creates a duplicate.
  Reproduced: after a lost reply, the retry queued the operation twice, both with a stream on the same client and
  from a fresh client.
- **Event stream.** `stream_events` raises on any HTTP error status when it reconnects (`:864-866`); only
  transport errors reconnect, after a fixed 1 s (`:874-878`). A 503 `identity_provider_unavailable`, which the
  server documents as retryable, and a proxy's 502 both ended the iterator for good (reproduced); 429 and 504
  take the same branch (inferred).

**Fix.** Base `If-Match` only on the caller's own read, per logical edit (e.g. a ContextVar that `edit_queue`
sets), and raise when an edit has no revision. Fix the key and revision once per logical call, retry transport
errors and 502-504 with exactly that pair, and put both on the raised exception. Treat 408, 429, 5xx and the
retryable 503 codes as transient: capped backoff with jitter, honouring `Retry-After`.

**Status.** Fixed. Each task, and each thread of the sync client, keeps its own revision: its own `queue()` read
sets it and only its own successful edits advance it; events and other responses never move it, and an edit with
none raises 428 `precondition_required` before anything is sent. A mutation keeps one `Idempotency-Key` and one
`If-Match` across its retries of network errors and 502-504 (backoff with jitter, `Retry-After` honoured); the
raised `ApiError` carries both, and `OutcomeUnknown` does when every try failed in the network. `stream_events`
reconnects through network errors, 408, 429 and 5xx with capped backoff. New tests in `tests/api/test_client.py`
cover each case. Those for If-Match and the stream fail on the old client's bugs: an edit made stale by events,
two tasks or two threads sharing a client, a stream through 503, 502 and 429. A lost reply followed by a 502 and
a 503 leaves one operation queued.

## Policy gaps for agents

These follow the design as written but work against the agent use case; each needs a decision before code.

### G7. The Edit role can get work executed (B2, B3)

`claim_next` takes the head of the queue for whichever execution is running, without checking who submitted the
operation or who holds the lease (HS `store/store.py:1212-1240`). Submit, replace, cancel and reorder need only
the Edit role and the lease.

- Reproduced: bob (Edit) gets 403 `insufficient_role` starting an execution. Once alice (Run) has started one and
  released the lease, bob takes the lease and his submission runs inside alice's execution.
- Stopping the execution (`store.py:1191`) and safe-stop (`store.py:1477`) need the caller's lease, as B3
  specifies. Renewal is unlimited (`store.py:975`; TTL up to 3600 s), so an Edit holder can keep the lease
  indefinitely and a Run operator can't stop bob's work without an Admin override.

**Options.** Exempt stop and safe-stop from the lease for Run principals, since they only reduce risk. Stop
admitting operations into an execution once the lease changes holder, or when the submitter lacks the Run role.

**Status.** Stops no longer need the lease: execution stop, safe-stop and the new stop-now need only the Run
role, so a Run operator can stop work an Edit holder controls. Admission is unchanged: bob's submission still
runs inside alice's execution.

### G8. Emergency stop is weak

- It needs the lease (G7).
- It is a deferred pause, then an abort once paused (HS `gateway.py:456-477`), with no deadline and no escalation
  (immediate pause, `re_halt`, `environment_destroy`). A deferred pause lands only at the plan's next checkpoint,
  so a move or sleep in progress runs to its end first. Reproduced with bluesky 1.16: a deferred pause requested
  1 s into `count(delay=5)`'s sleep landed 4.5 s later; an immediate pause landed at once.
- No route returns the active execution or the live attempt: `GET /attempts/{uid}` and
  `GET /queue-executions/{uid}` need the uid, and `GET /queue` has only `revision` and `operations`. A client must
  replay `/events` to find what to stop.

**Fix.** Routes for the current execution and live attempt. Escalate a safe-stop after a deadline, leaving the
attempt `unknown` unless its outcome is proven.

**Status.** Fixed, except for a deadline. `POST /attempts/{uid}/stop-now` sends an immediate pause, then an
abort; it needs no lease (G7) and also escalates a safe stop that has not landed. Against a real RE Manager, with
a count waiting 30 s between readings, the execution stopped 1.4 s after stop-now and 29.9 s after safe-stop.
`GET /active-execution` returns the running execution and its live attempt. A stop request on an attempt now
stops its execution once the attempt ends, whatever the outcome. Deferred: leaving a plan paused for an operator
to resume, stop, abort or halt (design doc, "Added later"). Still open: nothing escalates a stop automatically
after a deadline, or past an immediate pause (`re_halt`, `environment_destroy`).

### G9. No limits on what or how much a principal submits

- Any Edit principal can submit any catalog operation. There is no allowlist per role, principal or app
  registration; no queue-length cap, quota or rate limit; no maximum duration per operation.
- The lab catalog has only lower bounds (`v2-lab/profile/10-operations.py:39-40`): `num=2, delay=1e9` holds the
  instrument for about 31 years, and safe-stop can't end it, because `count` sleeps between checkpoints (G8).

**Fix.** Operation allowlists per role or principal; a queue cap, quotas and rate limits; a per-operation time
bound the gateway enforces through G8's escalation; and catalog guidance that remote-facing schemas must bound
cost (`maximum`, `maxItems`).

**Status.** The lab catalog is bounded: a count takes at most 100 readings 10 s apart, and a scan at most 50
energies (`v2-lab/profile/10-operations.py`). Out-of-range submissions get 422 `invalid_parameters` and write
nothing (reproduced on the lab stack). The rest is open.

### G10. A refusal counts as a failure (B3, B5)

Reproduced: 20 operations queued under `continue_on_failure` against a manager that refuses every
`queue_item_execute`. All 20 ended `failed` within 20 gateway steps, dispatch never blocked, and nothing ran.

**Fix.** Put a refused operation back at the head of the queue, and block dispatch after several refusals in a
row.

### G11. Parameter validation is shallow (A1, B2)

- Only the JSON Schema is checked, without a format checker (HS `catalog.py:145`). The decorator keeps only
  `model_json_schema()` and checks `additionalProperties` at the top level only
  (QS `annotation_decorator.py:399-415`). Reproduced: `num=3` against a "must be even" validator,
  `start_after="not-a-date"`, `sample_uid="not-a-uuid"` and an extra nested key were all accepted.
- The schema isn't reconciled with the function's signature (QS `annotation_decorator.py:417-421`). Reproduced:
  the schema advertised `num` defaulting to 5 while the plan ran with its own default of 1; a parameter the
  function requires but the schema leaves optional passes the door and then fails at dispatch.
- Device-map values aren't checked against the worker's namespace or the group's allowed devices. Reproduced:
  `det_does_not_exist` reached the plan as a string.
- With no dry run, each of these surfaces only as a `failed` attempt.

**Fix.** Validate with a format checker and require every object in the schema to be closed, or refuse request
models that have validators. Require each parameter without a default to be in `required`, and schema defaults to
equal the signature's (or have the controller fill them in). Check map values in `_process_plan` and when
building `plans_allowed`.

## Robustness and operations

### G12. Unbounded inputs and a single store lock (B1, B4, B6)

- Bodies are read whole and parsed before authentication, with no size limit (`server.py:104` sets none;
  `api/models.py:91, 98` are unbounded). Reproduced: malformed JSON with no token gets 422, not B1's 401.
- `GET /events?after=2**63` is a 500 `OverflowError` (reproduced). A `Last-Event-ID` longer than 4300 digits makes
  `int()` raise (`api/events.py:19, 74`; inferred); on `/events/stream` that failure comes after the 200 headers.
- Every read, every `events_after`, every write and the gateway share one `asyncio.Lock` on one connection
  (`store/store.py:273`). Each SSE stream queries the store on every wake-up (`api/events.py:82-99`), and there is
  no cap on streams per principal or overall. A Read token holding hundreds of streams delays the gateway's
  writes, safe-stop included (inferred).
- Parameter validation runs inside the write transaction, holding the lock (`api/queue.py:157, 201`).
- Idempotency records (`store/store.py:1767`) and events are never pruned, and each lease renewal adds one of
  each.
- A stream drains its backlog without checking token expiry (`api/events.py:89-90`).

**Fix.** A pure-ASGI body-size cap ahead of routing; bound `after` and `Last-Event-ID` to 2⁶³−1; per-principal
and global stream caps (429), with streams fanned out from one reader; validation before the transaction; expiry
for idempotency records; limits on the size and depth of `parameters` and the length of `operation_uids`.

### G13. RE Manager's history is never trimmed and is refetched whole on every change (B5)

`_refresh_history` fetches and re-indexes the entire history whenever `plan_history_uid` changes
(HS `gateway.py:368-379`), on the event loop. Measured on synthetic entries: decoding and indexing took 49 ms for
10k entries (7.7 MB) and 469 ms for 50k (38.6 MB).

**Fix.** Call `history_clear(size=N)`, which A3 keeps, once no unresolved attempt needs older entries; or fetch
only what is new.

### G14. Identity hardening (B1)

- For a generic OIDC provider, roles come from the `scope` claim by default (HS `identity/oidc.py:55`). On many
  identity providers (Keycloak optional client scopes, Okta's default policy, Auth0 without RBAC) that reflects
  what the client asked for, not what the user holds. The Entra preset uses `roles` and is not affected.
- Signing keys are refreshed inside requests, under a lock (`oidc.py:190-203`). Once the cached keys are a day old
  and the JWKS endpoint is unreachable, every request takes the lock, and once a minute one of them waits up to
  10 s on a fetch while the rest queue. Cached keys have no maximum age (`oidc.py:205-223`), so a key the IdP has
  rotated out stays valid for anyone who can block egress to it. An unknown `kid` gets 503 rather than 401.
- Only `exp`, `iss` and `aud` are required (`oidc.py:140`). Nothing checks `typ: at+jwt`, so an ID token with the
  right audience passes; `iat` is optional and `exp − iat` unbounded, and an event stream stays open until `exp`.

**Fix.** Require `roles_claim` explicitly for `provider = "oidc"`. Refresh keys in a background task and cap
their age. 401 for an unknown `kid`. Optional `typ` and `nonce` checks; require `iat` and cap token lifetime.

### G15. Audit, monitoring and file permissions (B4, D2)

- No access log ties a `request_id` to principal, route, status and error code. 401, 403, 409 and 412 aren't
  logged at all, though a 500's body asks the caller to report its `request_id`.
- `http_error` (`api/errors.py:248`; e.g. FastAPI's 400 for an unparsable body) is not one of the documented
  error codes.
- No metrics, and no alert when dispatch blocks or RE Manager becomes unreachable.
- Events record a bare principal id; nothing says whether it is a user or an app (`idtyp`), or on whose behalf it
  acted (`azp`, `act`). That matters once agents act for users.
- The SQLite file is created with the process umask (0644 under the usual 022); only the lock file is 0600
  (`store/files.py:62`). The lab's `state/` is 0755 (`v2-lab/lab.py:112`). Reproduced: `controller.sqlite` was
  `-rw-r--r--`, so any local account can read every operation, parameter, principal and recovery note.

**Fix.** A structured access log, metrics and alerts; record actor type and delegation; create the database 0600
and `state/` 0700, with `UMask=0077` and `StateDirectoryMode=0700` in D2's units.

## Smaller findings

### G16. A run-plan timeout counts as a proven failure (B5)

When `command_run_plan` times out, RE Manager records the item `failed` with "Timeout occurred while processing
the request" and goes idle (QS `manager.py:1182-1188, 1702-1711`), though the worker may have started the plan.
The gateway takes that message as proof of failure, since it is not in `_UNPROVEN_FAILURES`
(HS `gateway.py:26-32`). Inferred.

**Fix.** Add the message to `_UNPROVEN_FAILURES`.

**Status.** Fixed: the message is in `_UNPROVEN_FAILURES`, so the attempt becomes `unknown` and dispatch blocks
(`tests/test_gateway.py`).

### G17. `hold_lease` adopts a lease the same principal already holds (C1)

On `lease_held` it renews instead (HS `client/async_client.py:780-791`), so two sessions of one agent
registration both "hold" the lease; the first to exit releases it, and the other's `lost` stays `None`
(reproduced). Persistent 401 or 403 renewal errors are retried until the lease lapses (`:817-824`).

**Fix.** Raise `lease_held` unless the caller opts in to adoption; tie release to the grant this context made
(needs a server parameter); set `lost` on persistent 401 or 403.

### G18. The client treats redirects as success (C1)

It raises only on 4xx and 5xx (`async_client.py:127`). With an `http://` base URL behind an http→https redirect,
`release_lease()` returns having done nothing, after sending the token in clear (reproduced). A proxy's error page
surfaces as a pydantic `ValidationError` rather than `ApiError` (reproduced). `readiness()` sends the bearer token
to the unauthenticated `/ready` (`:176`).

**Fix.** Treat any non-2xx as an error, wrap body-parse failures in `ApiError`, don't authenticate `/ready`, and
refuse or warn on a non-loopback `http://` URL.

### G19. `stream_events()` starts from event 0 by default (C1)

`async_client.py:827`. An agent reacting to event types acts on old events unless it filters by its own
execution.

**Fix.** Require `after`, or default to the current tail.

### G20. The lab console mints Admin tokens for anyone (lab)

`v2-lab/ui/server.py` serves `POST /lab/token`, which mints a token for any user, role and lifetime, plus fault
endpoints that kill the worker or freeze the manager, all with no authentication and no Host or Origin check
(`:217-235, 296-299`). Reproduced on a copy of the lab: a `text/plain` POST from a foreign Origin got a
`QueueServer.Admin` token, and a 100-year token was minted for alice. Lab only, but it must not be copied into D2.

**Fix.** A per-run secret on `/lab/*`, Host and Origin checks, JSON plus a custom header on POSTs, and capped
lifetimes; or serve it on a `0700` unix socket.

### G21. Other lab setups that would be unsafe if copied (lab)

- `v2-lab/profile/user_group_permissions.yaml` allows every plan and device to both `primary` and `root`, so only
  the controller enforces the catalog.
- `demo.py`, `myscan.py` and `stress.py` use `http://` controller URLs; against a remote host they would send
  bearer tokens in clear.

## Missing compared with the design doc

### G22. A1-A3 are not upstream (A)

They exist only on `thopkins32/bluesky-queueserver`, branch `v2`, which still carries the stale ~8,000-line
`src/bluesky_queueserver/v2/` package.

### G23. Version coupling (D1)

`bluesky-queueserver` is still unpinned, both in the dependencies (`pyproject.toml:18`) and in the `v2` extra
(`:67`).

### G24. A clean checkout and CI can't run the v2 environments

- The `v2-*` pixi environments install queueserver from `./bluesky-queueserver` (`pixi.toml:44`), and the lab
  from `../bluesky-queueserver` and `../tst-sim-tools` (`v2-lab/pixi.toml:51-52, 89, 110`). Neither clone is in
  the repo, and neither path is ignored.
- CI (`.github/workflows/testing.yml`) runs only `py311`-`py314`, whose queueserver is upstream `main` without
  controller-fronted mode, so `tests/test_re_manager.py` always skips.

### G25. A1 spec items

- Descriptors carry no SHA-256 of the decorated function's source, so a change to the plan's code doesn't
  invalidate queued operations; the gateway compares only descriptor hashes.
- `qserver-list-plans-devices` emits no `operations:` section and no `catalog_sha256`. Descriptors travel inside
  each plan's `plans_allowed` entry, and the controller computes the catalog hash itself (HS `catalog.py:147`).
- `additionalProperties: false` is checked at the top level of the request schema only (G11).

### G26. A2 spec items

- Detection takes about 7 s rather than about 5 s: each poll cycle is a 0.5 s sleep plus a 0.5 s timeout, six are
  needed (QS `manager.py:764-768, 825`), and the kill task sleeps another 1 s. Reproduced: 6.93 s from SIGKILL to
  idle with no environment.
- The history entry gets `run_uids=[]` (QS `manager.py:707-713`) though `_re_run_list` still has them, and the
  gateway drops the last `re_runs` it saw when the attempt finishes (HS `gateway.py:556-557`).

**Fix.** Record `[r["uid"] for r in self._re_run_list]` and clear the list; detect by time elapsed since the
worker last answered; have the gateway fall back to the last `re_runs` it saw.

**Status.** G2's fix removed the kill task's 1 s sleeps, and one run then took 5.87 s from SIGKILL to idle with
no environment. The run UIDs are still dropped.

### G27. Client placement and walkthrough (C1)

- The client lives in this repo rather than `bluesky-queueserver-api`, so agents and remote users must install
  the whole server and its dependencies (`pyproject.toml:16-33`: fastapi, ldap3, pamela, pyzmq, sqlalchemy, …).
- No user docs. The acceptance walkthrough exists only as a pytest against the fake relay
  (`tests/api/test_client.py`), and it acknowledges recovery with a canned note; `v2-lab/demo.py` neither
  observes a failure nor acknowledges one.

### G28. Deployment reference and security review (D2)

None of it exists yet:

- Reference and systemd units: the service user, `UMask=0077`, `StateDirectoryMode=0700`, and a restart policy
  that also covers G3's exit status 0.
- TLS and a reverse proxy. The controller serves plain HTTP only, with no TLS or unix-socket listener, so on a
  multi-user host local accounts reach it over loopback and bypass the proxy's limits. The proxy needs rate
  limits; must pass `If-Match`, `Idempotency-Key`, `Last-Event-ID` and `ETag` through unchanged; must not gzip
  JSON, which weakens ETags (the client then silently ignores them, `async_client.py:129`); and needs buffering
  off and a read timeout above the heartbeat for SSE.
- Redis placement and persistence (G4).
- A backup and restore runbook (G5).
- Identity provider registration for users and agent apps. The lab's stand-in issuer uses `sub` and `scope`; the
  Entra preset (`oid`, `roles`), client-credentials agents and token refresh have never been exercised.
- The security review itself.

### G29. Documentation

No v2 user or operator docs. The catalog drops each plan's docstring (`DescriptorView`,
HS `api/models.py:314-326`, has no description), which agents need to choose and fill an operation.

## Checked and found correct

By reading the code unless marked reproduced, so these need no second look:

- Tokens: RS256 is fixed by a header check before any key fetch; a `kid` is required; keys come only from the
  configured `https` JWKS URI with redirects off, and only the public RSA members are loaded; the 60 s refetch
  limit holds against a flood of unknown `kid`s.
- Claims: exact `iss`; `aud`, `exp`, `nbf` and a future `iat` checked, with leeway capped at 300 s; a missing or
  over-long principal claim gives 401. Roles: string or list claims, case-sensitive, unknown roles ignored,
  highest role wins, none gives 403.
- Every route has the role the spec assigns, and the router-walk test exists (`tests/api/test_api_surface.py`).
- Idempotency keys are scoped to principal, method, path and key; a different fingerprint gives 409. `If-Match`
  gives 428, 400 or 412, and a 412 carries the current ETag. `X-Request-ID` is set even on 500s.
- `/ready` exposes only its five coarse fields; the docs pages are off; `/api/v2/openapi.json` needs the Read
  role; no CORS, and bearer-only auth leaves nothing for CSRF; `extra="forbid"` rejects a body that names an
  actor.
- A slow worker isn't taken for a dead one: a plan holding the GIL for 8.3 s completed with the environment still
  open (reproduced).
- Controller-fronted mode checks both the control and the info address, and the console publisher uses the info
  address.
- The client's token hook (a string, or a sync or async callable) runs on every request and stream reconnect; the
  sync wrapper handles Ctrl-C and running inside an event loop.

## Suggested order for what remains

1. Finish the worker-side boundary: the rest of G3 (socket directory check, saved lock), G25 and G26; commit the
   fork's fixes and take A1-A3 upstream (G22).
2. The client, before agents use it: G17, G18, G19.
3. Roles and stopping: G7's admission rule, and G8's deadline escalation.
4. Recovery and scheduling: G5, G10, G13.
5. Limits and hardening: the rest of G9, G11, G12, G14, G15.
6. Release and deployment: G4's Redis placement, G23, G24, G27, G28, G29; G20 and G21 when the lab becomes the
   D2 reference.
