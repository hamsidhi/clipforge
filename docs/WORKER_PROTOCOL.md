# Worker protocol (server ⇄ laptop), Phase 15b

The **server** (`deployment.mode: server`, always on) owns the database, the publishing queue, every platform login
and the Telegram bot. A **worker** (`deployment.mode: worker`, your laptop) takes heavy stages — download (home
internet: fewer YouTube bot checks), transcribe, LLM select, render — and sends the results back. Everything runs over
your Tailscale network (`https://<server>.<tailnet>.ts.net`, served by `tailscale serve`); nothing is public.

Code: server side `clipforge/server/{leases,dispatch,artifacts,worker_api}.py`, worker side `clipforge/server/worker.py`.
Tests: `tests/test_server.py`; real two-process run: `scripts/deploy_e2e.py` → [DEPLOY_TEST.md](DEPLOY_TEST.md).

## Authentication

* `clipforge server add-worker <name>` (on the server only — it's refused from a remote browser) creates
  `w_<hex>` + a key `cfw_<43 random chars>`, shown once. Only its SHA-256 is stored (`workernode.key_hash`).
* The worker sends `Authorization: Bearer cfw_…` on every call. This is **not** your password and not a browser
  session; `/api/worker/*` skips the session/CSRF middleware and every route checks the key itself.
* `clipforge server remove-worker <id>` revokes it at once; its running task goes back to the queue.
* A server that isn't in `server` mode answers 409 to every worker call.

## Tasks, leases, fencing

```
StageTask: queued ──claim──▶ leased(worker, fence=n, lease_until=now+lease_seconds) ──complete(fence=n)──▶ done
              ▲                   │ heartbeat (every heartbeat_seconds = 30) renews lease_until, reports progress
              └─ lease expired ───┘ → queued again; the next claim gets fence n+1
            cancelled (job cancelled) · failed (3 attempts) · fallback (no worker claimed it in time → the server does it)
```
* **Claim** is atomic (`BEGIN IMMEDIATE`): the oldest queued task whose stage is in the worker's capabilities.
  Every claim increments `fence`.
* **Heartbeat / complete / fail / artifact calls must carry the current fence.** A worker that slept through its lease
  gets 409 and drops its results — it can never overwrite newer work.
* **Complete is idempotent**: the same (task, fence) completing twice is accepted once (`{"already": true}`).
* **No job is lost**: an expired lease is re-queued (`reap`, every 15 s on the server); a task nobody claims within
  `deployment.worker_grace_minutes` (default 10) falls back to the server — except `hold` tasks (a download YouTube
  blocked on the server), which wait for a worker however long it takes.
* **No job is done twice**: one live task per (job, stage); results only with the current fence; publishing is
  separate and guarded (below).

## Endpoints

| Call | Body → answer |
|---|---|
| `POST /api/worker/hello` | `{capabilities, max_parallel, gpu, version}` → `{worker_id, name, server, lease_seconds, heartbeat_seconds}`. Also the "I'm online" ping (online = seen in the last 2 heartbeats + 30 s). |
| `POST /api/worker/claim` | `{capabilities}` → `{task: null}` or `{task: {id, fence, job_id, stage, video_id, source, section, title, config, ranks, force, force_select, manifest}}`. `config` is the job's full config (so fingerprints match); the worker only swaps in its own `output.dir` and `llm.ollama_url`. `manifest` = files the stage needs: `[{path, size, sha256}]`. |
| `POST /api/worker/heartbeat` | `{task_id, fence, progress, eta, message}` → `{status: "ok" \| "cancel"}`; 409 = lease lost → stop. |
| `GET /api/worker/artifacts/{task}/{path}?fence=` | download a workspace file (HTTP Range → resumable). |
| `GET /api/worker/staged/{task}/{path}?fence=` | `{size}` already uploaded (resume from there). |
| `PUT /api/worker/artifacts/{task}/{path}?fence=&offset=` | raw bytes (≤ 8 MB per chunk). Re-sending a chunk at the same offset is safe; a gap is refused. |
| `POST /api/worker/complete` | `{task_id, fence, files: [{path, size, sha256}], deleted: [path], state: {stage: {fingerprint, done_at}}, result}`. The server checks every staged file's size + sha256 (all-or-nothing), moves them into the workspace, merges the stage state, re-stamps render fingerprints, then continues the job. 422 = checksum/size problem (upload again), 409 = stale fence. |
| `POST /api/worker/fail` | `{task_id, fence, error, retryable, blocked}` → `{status: "requeued" \| "failed"}`. |
| `POST /api/worker/import` | `{video_id, source, title, settings}` → `{job_id, task_id, fence}`: a job the laptop ran while the server was unreachable; then push its files and `complete`. |
| `POST /api/server/fence` | `{reason}`: emergency standalone on the laptop → the server stops publishing until `clipforge server unfence`. |

## Files

* Only workspace files can move (`artifacts.ALLOWED`): `source.mp4, audio.wav, info.json, thumbnail.jpg,
  transcript.json, scenes.json, energy.json, llm_candidates.json, candidates.json, metadata.json/.csv, gallery.html,
  select_cache/*.json, clips/NN_*.mp4, clips/_work/NN/*`. No absolute paths, no `..`, no `:`; the resolved path must
  stay inside the workspace (checked twice). `state.json` is never sent as a file — only the finished stage's entry.
* Pull sets per stage: download → nothing; transcribe → info + audio; select → info, transcript, energy, candidates,
  LLM caches; render → everything. Files already on the worker with the same sha256 are skipped (a laptop that
  downloaded the source itself doesn't pull it back).
* Push = every file the stage created or changed (snapshot of size + mtime before/after), plus deleted files
  (e.g. a re-render renamed `03_old.mp4` → `03_new.mp4`).
* sha256 hashes are cached by size + mtime in `<workspace>/<video>/.hashes.json` (a 4 GB source is hashed once).
* Staging: `workspace/_incoming/<task>_<fence>/`, removed after commit; leftovers older than a day are cleaned up.

## Scheduling policy (server)

For each job, the first stage that isn't done yet (normal workspace cache: `pipeline.*_done`):
1. in `deployment.prefer_worker_for` and a capable worker is online → task for a worker; the job thread is released
2. download of a YouTube link and no worker online → the server downloads; if YouTube blocks it (bot check, 3 tries
   with 60 s / 120 s backoff) → a `hold` task for the laptop + a Telegram message with the options
3. otherwise the server runs that stage and the rest itself
`analyze` (seconds) always runs on the server. Re-render requests go to an online worker the same way.
Telegram shows where each job runs ("on hamza-laptop", "waiting for the laptop", "on clipforge-server (server)") and
when a worker joins or leaves.

## Publishing guard (exactly one publisher)

`server.dispatch.publishing_allowed()`: never on a worker; never on a server that was fenced. The scheduler
(`runner.run_once`), "publish now" and `clipforge publish-test` all check it. Queue claims stay unique per item
(`queue.claim_for_upload`), so even two schedulers on one database can't upload the same clip twice.

## Laptop life cycle

* `clipforge worker` (foreground) or inside `clipforge ui` when `deployment.mode: worker`; `clipforge worker install-task`
  = Windows task at logon + on wake, restarted if it stops.
* Ctrl+C / shutdown → the running task is handed back (`fail`, retryable) so the server re-queues it at once.
  Sleep can't be caught reliably on Windows — the lease simply expires (≤ lease_seconds) and the task is re-queued.
* Server unreachable: the worker retries with backoff (5 s → 5 min). You can still run jobs in the laptop's own app;
  they're uploaded to the server (`/api/worker/import`) the next time it connects. The laptop never publishes in
  worker mode.
* Server gone for good: `clipforge worker --standalone-emergency` fences the server (or, if it's unreachable, needs
  `--server-is-down` — you make sure it's stopped) and switches the laptop to standalone.
