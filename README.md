# Conduit

Personal-first data operations console: save database connections, load files into any of them,
transfer database to database, and watch the current state of everything. No history is kept.

**Status:** build steps 1-8 done, plus the docker.sock container check from step 9 (backend and UI both).
API, auth, encrypted credentials, connector plugin interface for 6 databases, connections CRUD **+ edit**,
background health checks, the transfer planner (ranked split-column recommendations), file inspect, the
**load engine** (DB to DB transfer and file load with parallel splits, progress + ETA, retry/resume/cancel,
a reject file), the **React frontend** (login, connections, the upload/transfer wizard, live jobs),
**Monitor** end to end (snapshot API, service checks — http/tcp/query/docker, all four creatable from the
screen — and host/job panels) and **Pipelines** end to end — saved read → transform → load recipes, a
3-column builder, and a **schedule editor** too: pipelines actually run on their own cron, with a real UI
to set it (`pages/pipelines/ScheduleModal.tsx`) — all reusing the same load engine, no mock data anywhere.
Not yet built: remote intake (S3/SFTP/URL, watched folder), Avro/ORC, and Control & Setup.

## Run it

Needs Docker (on Windows: Docker Desktop with WSL2, run these inside WSL).

```bash
mkdir -p data                     # so ./data is yours, not root's
cp .env.example .env
# put a key in CONDUIT_SECRET_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose up --build
```

- App: http://localhost:5173 · API + docs: http://localhost:8000/docs
- Admin login: user `admin`; the password is `CONDUIT_ADMIN_PASSWORD`, or if you left it empty a random one
  is printed once: `docker compose logs api | grep "generated password"`
- Files for SQLite/DuckDB connections, uploads and reject files live in `./data` (mounted at `/data`).
  File connections and file loads can only open paths inside `/data`. The containers run as uid/gid 1000
  (the WSL default); if `ls -n data` shows another owner, set `CONDUIT_UID` / `CONDUIT_GID` in `.env`.

## Layout

```
backend/app/
  main.py            FastAPI app, first-run admin seeding
  config.py          settings (env vars, prefix CONDUIT_)
  security.py        scrypt passwords, JWT, Fernet credential encryption
  models.py          users, connections (current health state lives on the row), jobs, service checks
  services.py        test/serialize/encrypt helpers shared by API and worker
  files.py           file inspect: columns, row estimate, preview (DuckDB + openpyxl)
  api/               auth, connections, files, jobs, monitor, pipelines
  connectors/        one class per database: connect, test, inspect (base.py is the interface)
  engine/            the load engine
    spec.py            job request (what the wizard sends), pipeline step / schema models, bound parsing
    runner.py          planning, parallel splits, retry/resume/cancel, progress, ETA, rejects
    adapters/          per database: split planning, streaming reads, writers (one file each)
    filesource.py      CSV/TSV/JSON/NDJSON/Parquet/Excel as a source
    transform.py       column mapping, type casts, date formats
    steps.py           pipeline transform steps: rename/cast/filter/derive/split, SQL, Python
    schedule.py        pipeline scheduling: cron validation, next-run math, the due/claim logic
    types.py, ddl.py   one logical type vocabulary and per-database CREATE TABLE
  compare/           source vs target diffs: spec, DuckDB diff engine, result exports
  monitor/           service checks (HTTP / TCP / query) and host resources (psutil)
  worker/            Celery app; runs jobs; beat runs health checks, due service checks and due pipelines
backend/alembic/     migrations
backend/tests/       pytest

frontend/src/
  lib/               api.ts (typed client), auth.tsx, theme.tsx, toast.tsx, format.ts, highlight.ts
  ui/                Button, Input, Card, Modal, Badge, ProgressBar, SegmentedControl, Toggle ...
  shell/             AppShell (header + collapsible sidebar), Logo
  pages/             Login, Connections (+ AddConnectionModal, also used to edit), Jobs (+ jobs/JobDetail, SSE-driven),
                     Monitor (+ monitor/ServiceCheckModal), Pipelines (+ pipelines/*), ComingSoon (Control)
    upload/          the 4-step wizard: Wizard, Step1Source .. Step4Review, jobBuilder.ts (state -> JobIn)
    pipelines/       Builder (3-column: steps sidebar, step editor, live preview), NewPipelineModal,
                     StepEditor (one editor per step kind), SourceTargetEditor (shared read/load editors),
                     ScheduleModal (cron + enabled, with a "run a full preview now" fix-it path)
  theme.css          tokens ported verbatim from frontend/design/Conduit v2.dc.html
frontend/design/      the original Claude Design prototype (reference only, not built/served)
```

## API (all under /api, bearer token except login and health)

| Method | Path | Purpose |
|---|---|---|
| POST | /auth/login | username + password, returns token |
| GET | /auth/me | current user |
| GET | /meta/db-types | database types for the add-connection flow |
| GET/POST | /connections | list / create |
| POST | /connections/test | test unsaved settings |
| POST | /connections/test-all | test every saved connection now |
| GET/PATCH/DELETE | /connections/{id} | read / update / delete |
| POST | /connections/{id}/test | test one, updates its state |
| GET | /connections/{id}/tables | tables with catalog row estimates |
| GET | /connections/{id}/tables/{table}/inspect | columns, keys/indexes, ranked split candidates |
| POST | /files/upload | browser upload into `/data/uploads/...`, returns the path (max 2 GB) |
| POST | /files/inspect | inspect a file under an allowed root: columns, row estimate, preview |
| POST | /jobs/plan | dry run: columns, DDL, splits, row estimate, timed sample. Writes nothing |
| POST | /jobs | create and start a job (transfer or file load) |
| GET | /jobs | running first, then the latest finished ones |
| GET | /jobs/{id} | state, per-split progress, rows/s, ETA, plan, last log lines |
| GET | /jobs/{id}/events | Server-Sent Events, one update a second (`?token=<jwt>`) |
| GET | /jobs/{id}/log | last log lines |
| POST | /jobs/{id}/cancel | stop after the current batches |
| POST | /jobs/{id}/retry | continue a failed, cancelled or stalled job |
| GET | /jobs/{id}/rejects | download the reject file (CSV) |
| DELETE | /jobs/{id} | remove a finished job |
| GET | /monitor | everything the Monitor screen shows, in one call (poll every few seconds) |
| POST | /monitor/refresh | test every connection and run every service check now, return the snapshot |
| GET/POST | /monitor/checks | list / create service checks |
| PATCH/DELETE | /monitor/checks/{id} | edit (re-validated, result reset) / delete |
| POST | /monitor/checks/{id}/run | run one check now (409 if it is running) |
| GET/POST | /pipelines | list / create a saved pipeline |
| GET/PATCH/DELETE | /pipelines/{id} | read / update (source/steps changes clear the cached schema; schedule/enabled changes recompute `next_run_at`) / delete |
| POST | /pipelines/{id}/preview | sample the source, run the step chain, return columns + rows; a full-chain preview caches the schema `/run` needs |
| POST | /pipelines/{id}/run | run it now as a job (`kind: "pipeline"`); 422 if there's no cached schema yet (preview first) |
| POST | /compare/columns | both sides' columns, the automatic pairing (same names) and the suggested key |
| GET/POST | /compare | list the latest comparisons / start one (`CompareIn`; bad tables, columns or keys fail with 422 right away) |
| GET | /compare/{id} | status, phase, rows read per side, and the summary when done |
| GET | /compare/{id}/rows | `?set=missing\|extra\|mismatched\|dup_source\|dup_target&offset=&limit=`: one page of a result set |
| GET | /compare/{id}/export | `?sets=a,b&columns=all\|keys\|a,b&format=csv\|xlsx\|parquet\|json&rows=all\|sample`: a file (several sets: a .zip, Excel: one sheet each) |
| POST | /compare/{id}/cancel | stop it |
| DELETE | /compare/{id} | remove it and its result files |
| GET | /health | liveness + app DB check |

Secrets (passwords, MongoDB URIs) are encrypted with `CONDUIT_SECRET_KEY` and never returned.
Passwords omitted on `PATCH` keep their stored value.

## Notes

- **ClickHouse uses the HTTP(S) interface** (ports 8123/8443), not the native port 9000/9440. It gives the
  load engine Arrow batches in and out.
- **Table listing spans every schema/database the connection's credentials can see**, not just the one
  (if any) configured on the connection. Postgres, DuckDB, and SQLite already worked this way (Postgres
  across all non-system schemas in its one database; DuckDB across all attached schemas; SQLite has only
  one). MySQL/MariaDB, ClickHouse, and MongoDB previously listed only the configured `database` (or a
  driver-resolved default) — a `.../connections/{id}/tables` call now lists tables from every schema
  MySQL's `information_schema.tables` exposes to the user, every ClickHouse database its RBAC grants let
  it see, and every MongoDB database `listDatabases` returns (or, for MongoDB, the URI's own database
  when `listDatabases` isn't permitted). This is a pure listing change: table names were already
  schema-qualified (`schema.table`, `db.table`) end to end, so DDL generation, inspect, and the source
  and target pickers in upload/load, transfer preview & plan, and target & options all work unchanged —
  they just now have more tables to choose from.
- **Editing a connection**: the Connections screen's row menu has an Edit action alongside Delete
  (`AddConnectionModal.tsx` now doubles as an edit dialog — `connection` prop present skips straight to the
  form, hides "Change type" since `PATCH /connections/{id}` can't change `type`, and pre-fills every
  non-secret field from the saved `config`). Secret fields (`password` for host-kind connectors, the whole
  `uri` for MongoDB) can never be read back from the API, so they always start blank with a "blank keeps
  the current value" hint; leaving them blank on save matches the backend's own merge behavior (a secret
  omitted or empty in the `PATCH` body keeps its stored value). The backend endpoint and the API client's
  `connectionsApi.update()` both already existed — this was a pure frontend gap.
- **Health checks** run in the `beat` service every `CONDUIT_HEALTH_CHECK_INTERVAL_S` seconds and overwrite the
  connection's state. Slower than 500 ms reports "Degraded".
- Run exactly one `beat`; scale `worker` freely.
- **Transfer planner**: `.../tables/{table}/inspect` ranks split-column candidates (recommended /
  alternative / not_advised) from cheap catalog metadata only — indexes/keys plus physical-order
  stats where the source exposes them (e.g. Postgres `pg_stats.correlation`, ClickHouse's sorting
  key). No table is scanned to build the recommendation.
- **File inspect** supports CSV/TSV, JSON/NDJSON and Parquet via DuckDB, and Excel via openpyxl.
  Row counts over 200 MB of CSV/JSON are not pre-counted (would mean a full read); Parquet and
  Excel row counts are always exact since both formats expose them without scanning.

## Load engine

A job is `source` (table, query or file) + `target` (connection, table, mode) + `options`. Send it to
`POST /jobs/plan` first: it returns the columns and types it will create, the `CREATE TABLE` per database,
the splits, a row estimate and a timed sample of the read/write speed.

**Options** (`options`)

| Option | Meaning |
|---|---|
| `strategy` | `full`, `id` (from/to inclusive), `time` (from inclusive, to exclusive), `watermark` (only rows above the last finished run) |
| `split_column` | column cut into ranges that run in parallel; use the one `/inspect` recommends |
| `lo`, `hi`, `timezone` | bounds for `id` / `time`; naive times are read in `timezone` |
| `workers` | parallel splits at once (server cap `CONDUIT_MAX_WORKERS_PER_JOB`, default 32) |
| `splits` / `split_rows` | how many splits, or aim for this many rows per split |
| `read_chunk` | rows fetched per round trip |
| `write_batch` | rows per insert and commit |
| `max_rejects`, `retries` | stop after N bad rows (-1 = never); attempts per split, with backoff |

Target `mode`: `append`, `replace` (truncates first) or `upsert` (needs `keys`).

**How a job runs.** Each split is one thread: a reader thread prefetches Arrow batches while the writer
inserts them in `write_batch` rows per transaction. A failed batch is bisected to isolate the bad rows, which
go to the reject file (`/data/rejects/<job id>.rejects.csv`); the rest are written. Transient errors are
retried with backoff; the counters live in the app database, so progress survives a restart. The ETA comes
from the rows/s of the last 30 seconds. Only the latest 10 finished jobs are kept (`CONDUIT_KEEP_FINISHED_JOBS`).

**Retry, resume, stalled.** `retry` keeps finished splits and continues the others after their last committed
key. If a worker is killed (`docker compose restart worker`), the job shows as **stalled** after 90 s of
silence: press retry. A redelivered task never runs a job twice (heartbeat guard).

**Guarantees and limits**

- Append is at-least-once; upsert is idempotent. A split on a non-unique column that fails midway restarts
  only if nothing was written yet, otherwise the job fails with a clear message. The reject file can contain
  a row twice after a resume.
- Each split reads its own snapshot: rows changing while a multi-split job runs can be missed or read twice
  at split borders.
- Replace empties the target first; it stays incomplete until the job finishes.
- ClickHouse: naive `DateTime` values move as UTC instants; upsert creates a ReplacingMergeTree (read with
  `FINAL`); large targets get a monthly partition suggestion. MySQL/MariaDB writes are pure-Python
  (about 26k rows/s per writer when measured), so raise `workers`. SQLite and DuckDB accept one writer at a time.
- MongoDB: `_id` ObjectIds become strings on the way out; append is at-least-once, upsert is by key.
- Excel: `.xlsx` only (`.xls` is refused with a message). Avro/ORC, S3/SFTP/URL intake and watched folders
  are not built yet.

## Monitor

Current state only: every value is overwritten by the next check and nothing is kept as history.
`GET /monitor` returns one JSON document with everything the screen shows:

| Key | Content |
|---|---|
| `connections` | every saved connection with its current health (same objects as `GET /connections`) |
| `host` | hostname, OS, vCPUs; `cpu` (percent, load average), `memory`, `disks`; each with `level` `ok`/`high` |
| `jobs` | `running`, `queued`, `slots_total` (Celery worker slots, `null` if unknowable), `worker_online`, `active` (job snapshots) |
| `services` | service checks with their latest result |
| `summary` | counts for the panel headers ("3 up · 1 down") |

**Service checks** watch things outside Conduit. Each has a `kind`, an `interval_s` (10 s to 1 h, default 30)
and an `expect` value:

| `kind` | `target` | `expect` | Result |
|---|---|---|---|
| `http` | URL | status code, default 200 | `up` on the expected status (redirects are not followed, so `302` is compared like any status), `warn` if slower than 2 s, `down` otherwise |
| `tcp` | `host:port` | slowest acceptable connect time in ms, default 1000 | `up` if it connects in time, `warn` if slower, `down` if refused / timed out |
| `query` | a saved SQL connection + a `SELECT` / `WITH` query | expected row count, empty = any | runs `COUNT(*)` over your query; only single read statements are accepted; MongoDB is not supported |
| `docker` | a container name or ID | empty/`running` (just up), or `healthy` (require a passing `HEALTHCHECK`) | `up` if running (`healthy` too, when asked); `warn` if running but unhealthy, or if a `HEALTHCHECK` is still in its `starting` grace period; `down` if stopped, not found, or (for `healthy`) no `HEALTHCHECK` is defined at all |

A new or edited check runs at once; after that, beat runs whatever is due every 5 seconds, so the effective
interval is the setting plus up to 5 s. A check nobody ran for three intervals is flagged `stale` (is `beat`
up?). Set `verify_tls` to `false` for internal HTTPS with a self-signed certificate.

**Host resources** are what the API process sees: inside Docker that is the Docker host (on Windows, the WSL2
VM), not Windows itself. `Data` (the `/data` mount) is always shown; add more with
`CONDUIT_MONITOR_DISKS="/mnt/big:Big disk,/srv"` (paths as seen inside the container, so mount them
read-only in `docker-compose.yml` first). `CONDUIT_MONITOR_WARN_PCT` (default 85) is where `level` turns
`high`. `CONDUIT_CHECK_TIMEOUT_S` (default 8) is the HTTP/TCP/docker timeout.

**Docker checks** talk straight to the Docker Engine API over its unix socket (no `docker` CLI involved,
stdlib-only) — `GET /containers/{name}/json`, read `State.Status` and `State.Health.Status`. They only work
if that socket is actually reachable from wherever the check runs: the API process for a new/edited check's
first run, and the **worker** container for every scheduled re-run (beat's `conduit.monitor.run_due` task
runs there) — so mount `/var/run/docker.sock:/var/run/docker.sock:ro` read-only into *both* `api` and
`worker` in `docker-compose.yml` (commented-out example there) if you want to use this kind. Nothing is
mounted by default: it's optional, and it grants read access to every container name, image, and running
command on the host, so only enable it if you trust everyone who can create a check here.
`CONDUIT_DOCKER_SOCKET` (default `/var/run/docker.sock`) points at a different socket path.

Not built: alerts/notifications.

## Pipelines

A pipeline is a saved, named `source -> steps -> target` recipe. Running one creates an ordinary job
(`kind: "pipeline"`, visible in the Jobs list and over SSE like any other job) — the pipeline just
remembers the recipe and the schema it last produced, and points at its most recent job.

**The read and the target stay exactly like an ad-hoc job**: `source` (a table, query or file) and
`target` (connection, table, mode) are the same shapes `POST /jobs` takes. `steps` sit between them and
run once per batch, in order, over the same Arrow batches the load engine already streams — there's no
separate pipeline execution engine. Every visual step compiles to one generated SQL statement run in an
in-process DuckDB; a `sql` step is your own statement; a `python` step execs a `def transform(df): ...`
against a pandas DataFrame.

| `kind` | `config` | Does |
|---|---|---|
| `rename` | `{"mapping": {"old": "new", ...}}` | rename columns |
| `cast` | `{"mapping": {"col": "SQL_TYPE", ...}}` | `CAST` columns (`DOUBLE`, `DECIMAL(12,2)`, `TIMESTAMP`, ...) |
| `filter` | `{"expr": "amount > 0"}` | keep rows matching a SQL boolean expression |
| `derive` | `{"column": "name", "expr": "..."}` | add one computed column |
| `split` | `{"column": "c", "into": [{"name": "n", "expr": "..."}], "drop_source": false}` | add one or more columns computed from an existing one (e.g. `posted_at` → `posted_date`) |
| `sql` | `{"sql": "SELECT ... FROM {{ input }} ..."}` | your own DuckDB SQL over the previous step's output |
| `python` | `{"code": "def transform(df):\n    ...\n    return df"}` | your own pandas function over the previous step's output |

**Preview, then run.** `POST /pipelines/{id}/preview` samples the real source, runs the step chain, and
returns the resulting columns and rows — this is also how the "Load" step knows what it's writing: a
full-chain preview (no `upto_step`) caches the resulting column list on the pipeline, and `/run` refuses
with a 422 until that cache exists. Editing `source`, `target`, `options` or `steps` clears the cache, so
a pipeline can never run against a schema it hasn't actually produced. `upto_step` previews only up to
(and including) one step, for a builder UI that shows the table after each step without re-caching.
There's no separate target column-mapping step (unlike the wizard): whatever columns the step chain
produces become the target table's columns, one for one.

**Scheduling.** `schedule` is a cron expression (`croniter` syntax: 5 fields like `0 2 * * *`, or a
shortcut like `@daily` / `@hourly`), validated on save; `enabled` gates it. Saving a schedule computes
`next_run_at` (returned on the pipeline) but never runs anything by itself — a schedule only ever fires
on its own clock, since an immediate run on save could write to a real target. A Celery beat task
(`conduit.pipelines.run_due`, every 30 s) queues a job for every pipeline that is enabled, has a
`schedule`, already has a cached preview (the same guard `/run` enforces manually), and whose
`next_run_at` has passed, using an optimistic claim (`schedule_claimed_at`, stale after 60 s) so two beat
ticks never queue the same firing twice. A scheduled run has no request/session user, so it runs as the
oldest active account (this is a single-admin tool). After every firing — success or failure —
`next_run_at` is recomputed from the cron expression, so a broken pipeline just waits for its next slot
instead of retrying every tick; check the pipeline's `last_run` (or the Jobs list) to see whether a
scheduled firing actually succeeded.

The **schedule editor** (`pages/pipelines/ScheduleModal.tsx`, opened from a "Schedule" button in the
builder's top bar, next to Refresh preview / Run pipeline) sets `schedule` and `enabled` with cron presets
(hourly/daily/weekly/monthly) plus a free-text field, shows `next_run_at` once one is set, and — since a
schedule that never got a full preview will just silently never fire — surfaces that state with a
"Run a full preview now" button right in the dialog instead of sending you back into the builder to find
the fix. The Pipelines list shows the schedule and a live "next in Xh" / "paused" / "no preview yet"
column, with pause/resume in the row menu.

**Known limits (v1)**
- **Per batch, not over the whole result.** A step sees one batch at a time (same batches the rest of
  the load engine reads/writes), never the complete source. `GROUP BY`, `DISTINCT` and window functions
  across the *whole* table are not supported — an aggregation-shaped pipeline (an hourly rollup, say)
  needs a different execution model this version doesn't have. Row-wise/batch-safe SQL (a `LEFT JOIN`
  against a small reference table, computed columns) works fine.
- **No parallel splits.** `options.split_column` is rejected when `steps` is non-empty: a pipeline
  always reads its source in one piece. (`workers` / chunk sizes still apply to everything else.)
  Splittable, incremental pipelines are a later iteration.
- **Python steps are not sandboxed.** `transform(df)` runs with the Celery worker's own privileges —
  no separate venv, no network/filesystem restriction. Fine for your own pipelines on your own machine;
  don't paste in a pipeline you wouldn't otherwise just run.
- **Progress/ETA is approximate** for a pipeline with a `filter` step: the row-count estimate comes from
  the raw source, not the post-filter output, so the progress bar can look like it plateaus near the end.
- **Remote intake isn't built.** A pipeline's `source` is today's table/query/file — S3, SFTP, a URL and
  a watched folder (all in the design) aren't sources yet.

## Compare

Checks that a target holds exactly what a source has. Pick a source (a table, a SQL query or a file) and a target
(any saved connection, table or query) on the **Compare** page, or press **Verify** on a finished job to open it
pre-filled with that load's source, target and column mapping. Columns with the same name (any case) are paired
automatically; renamed ones can be paired by hand, and unpaired columns are listed but not compared.

**How rows are matched.** By key columns, defaulting to the target table's primary key (else the source's). That
gives three result sets: **missing in target**, **extra in target**, and **value differences** (same key, at least
one value differs, with source → target side by side and the list of differing columns). With no key, whole rows
are compared as multisets (`EXCEPT ALL`), so duplicates count and a changed row is one missing plus one extra row.
**Duplicates** in the source and in the target are reported as separate sets (keys, or whole rows, seen more
than once).

| Option | Default | Meaning |
|---|---|---|
| `ignore_nulls` | off | a NULL on either side is not a difference (key matching only) |
| `check_duplicates` | on | report duplicate keys / rows on each side |
| `trim_text` | off | ignore leading / trailing whitespace |
| `case_insensitive` | off | `'ABC'` equals `'abc'` |
| `float_decimals` | 6 | FLOAT / DOUBLE are rounded first, so `0.1 + 0.2` equals `0.3` |
| `sample_rows` | 100 | rows per set for a "sample only" export |

**Types.** Values are compared by value, not by text: each column pair is cast to one common type (integers and
decimals exactly, anything with a float as DOUBLE, DATE vs TIMESTAMP as TIMESTAMP, time-zone-aware times as UTC).
Text compared with a typed column is parsed as that type (`' 1.50'` equals `1.5`); text that does not parse is
NULL and so a difference. Anything else is compared as text.

**How it runs.** Like a load job: in the Celery worker (or a thread with `CONDUIT_JOB_RUNNER=thread`), both sides
read at the same time through the load engine's adapters into a scratch DuckDB file under `/data/compare/<id>/`,
then diffed in SQL. The scratch copy is deleted as soon as the diff is done; only the result sets are kept, and only
for the latest `CONDUIT_KEEP_COMPARISONS` (default 10) comparisons. Results can be paged in the UI and exported as
CSV (default), Excel, Parquet or NDJSON, everything or just the sample, all compared columns or only some (key
columns always included).

**Limits.** The scratch copy needs free disk about the size of the compared columns of both sides: compare a
slice of a huge table with the WHERE filters. Each side is read as it is at that moment, so rows changing during
the compare show up as differences. Rows with a NULL key never match. If a key is not unique, each copy is matched
with every copy on the other side (a note says so).

## Frontend

React + TypeScript + Vite, ported from the `frontend/design/Conduit v2.dc.html` prototype (same oklch
color tokens, Geist / Geist Mono / Material Symbols Rounded, dark default + light). Everything renders
from the real API — there is no mock data layer. `docker compose` serves the production build through
nginx on :5173, proxying `/api` to the `api` service (`frontend/nginx.conf`; SSE works there too,
`proxy_buffering off`). For frontend-only iteration with hot reload, run the backend via Docker and the
UI separately: `cd frontend && npm install && npm run dev` (Vite's dev server proxies `/api` to
`localhost:8000`, see `vite.config.ts`) — stop the `frontend` container first, or both will fight over
:5173.

**Adapted from the mockup, deliberately, because the backend doesn't do these (yet):**
- Remote intake (S3 / SFTP / URL) is a disabled placeholder tab — not built server-side. Upload and
  Server folder are both real (the latter needed a small new endpoint, `GET /files/list`).
- No SSH tunnel toggle or per-connection "include in health checks" toggle: neither exists in the
  connector/health-check code, so the add-connection form only exposes real fields (SSL mode,
  `read_only` / `create_if_missing` for file databases, `tls` / `auth_source` for MongoDB).
- A file's "Detected format" panel shows what DuckDB actually auto-detected; only `header` and
  `skip_rows` are editable overrides (both real `FileIn` fields), not delimiter/encoding/sheet.
- Column types default to "(auto)" (omits `type` from the `ColumnMap`, so the backend's own inference
  wins) rather than guessing a logical type from a raw driver type string client-side.
- The DB-source step has no 100-row data preview: `/connections/{id}/tables/{table}/inspect` returns
  schema and split candidates, not sample rows, and there's no target yet to run a real `/jobs/plan`
  sample against at that point in the wizard. The sample preview appears once a target is chosen,
  from the real dry-run in Review.
- Full-load transfers can still run in parallel: picking a `ctid` / `_partition_id` planner
  recommendation sets `split_column` while `strategy` stays `full` (no value bounds needed) — this is
  how Postgres's no-index fallback and ClickHouse's partition split actually work.
- The Add-service-check modal's "Query" type replaces the mockup's single freeform target field with
  a connection picker + query textarea, matching the real `CheckIn` schema (a query check needs a
  saved SQL connection, not a URL string). The mockup's "Timeout" dropdown is dropped — there's no
  per-check timeout field server-side, only the global `CONDUIT_CHECK_TIMEOUT_S`. A fourth "Docker" type
  was added (not in the mockup at all — the `docker` check kind is a step 9 backend addition): its
  `expect` field is a Running/Healthy dropdown rather than free text, since that's all the API accepts
  for this kind.
- "Save as pipeline" (in the upload wizard) still isn't wired up — Pipelines has its own "New pipeline"
  entry point instead, since a pipeline needs a source and target up front (`POST /pipelines` requires
  both), unlike the wizard which only finalizes them in step 3. The design-system screen is the one
  remaining `ComingSoon` placeholder besides Control & Setup, matching the sidebar's "SOON" badge.
- Pipelines has no manual step reordering (drag-and-drop wasn't in the mockup either — new steps always
  append before Load) and no per-step "up next" column-name autocomplete (the input columns before a
  step are only reliably known once a preview has run, so free-text entry is used everywhere instead).
- A pipeline is created immediately (not staged client-side like the wizard) because `/preview` and
  `/run` are both scoped by pipeline ID: the "New pipeline" modal collects just enough (name, source,
  target) to `POST /pipelines` successfully, then every further edit in the builder is auto-saved
  (debounced ~700 ms) and immediately re-previewed — there's no separate unsaved-changes state to track,
  and the visible "Saved" / "Saving…" indicator is literally true rather than a mockup guess.

## Develop without Docker

```bash
cd backend
pip install -e ".[dev]"
python -m pytest                       # SQLite app DB; live tests use local PostgreSQL :5432, MariaDB/MySQL :3306,
                                       # ClickHouse :8123 (user conduit / default, database conduit) and skip if absent
export CONDUIT_JOB_RUNNER=thread       # optional: run jobs inside the API process, no Celery worker needed
export CONDUIT_DATABASE_URL=postgresql+psycopg://conduit:conduit@localhost:5432/conduit
export CONDUIT_SECRET_KEY=...          # see above
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.worker.celery_app worker --loglevel=info
celery -A app.worker.celery_app beat --loglevel=info
```

## Not yet verified

- Tested against live servers: PostgreSQL 16, MariaDB 10.11, ClickHouse 24.8, SQLite, DuckDB, Redis.
  **MongoDB has only run against `mongomock`** (an in-memory stand-in), never a real server, and MySQL 8
  itself was not tried (MariaDB was). Run one job against each of your real servers before you rely on it.
- The multi-schema table-listing fix above was verified live against a real MariaDB 10.11 (a second
  schema stayed visible even with `database` pinned to the first). The ClickHouse fix is the same
  one-line change to a `system.tables` query and follows the same reasoning as MySQL/MariaDB, but wasn't
  re-verified against a live ClickHouse server in this pass. The MongoDB fix was verified against
  `mongomock` only, consistent with the MongoDB caveat above — a real server may behave differently if
  `listDatabases` is restricted in some way `mongomock` doesn't model.
- Verified live end to end through `docker compose up --build` (all 6 services, including `frontend`):
  signed in, added and tested a real SQLite connection, uploaded a CSV through the wizard, generated
  DDL against it, ran the load, and watched it succeed over the real SSE stream — zero console errors,
  light and dark theme both checked. Only exercised against SQLite this way so far; the same wizard
  path against Postgres/ClickHouse/MySQL/MongoDB targets has not been clicked through by hand yet
  (the underlying engine has, per above — it's the click-path that's unverified).
- Monitor backend: verified live (API + Celery worker + beat on PostgreSQL 16 and Redis): checks refreshed
  on their interval, a stopped web server and TCP port turned `down`, a query check ran on a real
  connection, worker slots came from the live Celery worker, migration 0003 ran up and down. Not tried:
  HTTPS with a self-signed certificate, and disks other than `/data`.
- Monitor screen: verified live through `docker compose` — added a real HTTP check (an actual outbound
  request to httpbin.org came back `200 OK`), a real TCP check against a closed local port (came back
  `down · connection refused` on the next beat tick, no page interaction needed), ran the Refresh button,
  used the row menu (run now / disable / delete), and opened the Query check form (connection picker +
  SQL textarea). Host resources panel showed the real container's CPU/memory/disk. Zero console errors.
  `CONDUIT_MONITOR_DISKS` parsing has a known bug on Windows-style paths (a bare drive-letter colon is
  read as the `path:label` separator) — irrelevant in the Linux containers this actually ships in, so
  left alone; showed up only as one failing test when running `pytest` natively on Windows.
- Pipelines backend: verified live — every step kind (rename/cast/filter/derive/split/sql/python) against
  plain Arrow tables; full create → preview → run against an embedded SQLite source and target (rows
  landed transformed, filtered rows dropped correctly); a full create → preview → run against a real,
  network-connected PostgreSQL connection through the real Celery worker (not the in-test thread runner) —
  the target table's DDL, the row filtering and the derived column all came out correct; editing a
  pipeline's steps correctly clears its cached schema and blocks `/run` until it's previewed again.
  Not tried live: a `python` step against MySQL/MariaDB or ClickHouse source/target (the step engine
  itself is database-agnostic — it only ever sees Arrow batches — so this is expected to work, just
  unverified), and anything past the "known limits" above (aggregating pipelines, scheduling, remote
  intake).
- Pipeline scheduling backend: verified live through the real Celery beat + worker against PostgreSQL 16
  and Redis — created a pipeline with a `@daily` schedule, previewed it (caching its schema), forced it
  overdue directly in the database, and watched the real `pipelines-due` beat tick (every 30 s) queue and
  run an actual job through `conduit.jobs.run`: 3 rows read, 2 written (the negative-amount row correctly
  filtered), `next_run_at` advanced to the following day, `schedule_claimed_at` cleared afterward. Also
  confirmed a pipeline that's overdue but was never previewed is left alone (no job, `next_run_at`
  untouched) — the same guard `/run` enforces manually — and that a pipeline another tick already
  claimed is skipped too. Not tried: a schedule firing while its own previous run is still in progress
  (the claim logic is unit-tested, but not exercised against a real slow job), and DST edge cases in the
  cron math (`croniter` handles these; not specifically checked here).
- Pipelines screen: verified live through `docker compose` — created a pipeline from scratch (SQLite
  source and target), added a `filter` step and a `rename` step, watched the live preview update with
  the real filtered/renamed data after each edit, then ran it: the resulting job showed "3/4 rows"
  (the 4th correctly dropped by the filter) and a real engine log (`CREATE TABLE`, split start/finish,
  `3 rows written, 0 rejected`). Caught and fixed one real bug in this pass: adding a blank row in the
  rename/cast editors was silently discarded (deriving the editable rows straight from the `{old: new}`
  mapping dict can't represent an in-progress empty key) — rows are now local component state, only
  collapsed into the outgoing mapping once both sides are filled in. Zero console errors after the fix.
  Not tried: a `split`, `sql` or `python` step through the UI (exercised directly against the API in the
  backend's own tests, per above, but not clicked through), and any target besides SQLite.
- Schedule editor: verified live through the real Celery beat + worker (not just the API) — set an
  invalid cron and got the exact server validation message back in the dialog; picked the "Daily at
  midnight" preset and saved; forced the cron to `* * * * *` and watched a real beat tick queue and
  finish a real job within its next 30 s window, with the correct filtered row count (`rows_written: 2`
  of 3 read) and `next_run_at` advancing. Found and fixed a real bug in the process, more consequential
  than it first looked: the builder's auto-preview (and the "Refresh preview" button) scope to whatever
  step is currently selected in the sidebar, and adding or editing a step leaves that step selected —
  not "Load" — so a completely ordinary editing session could go on indefinitely without ever sending
  the `upto_step`-omitted request that caches the schema `/run` and every scheduled firing require. In
  practice this meant **Run pipeline** could stay silently unusable, and a saved schedule could stay
  silently dead, with nothing in the UI explaining why. Fixed by having "Run pipeline" force a real
  full-chain preview immediately before running, regardless of sidebar selection, and by giving the
  schedule dialog's "no cached preview" warning its own one-click fix. Confirmed clean (zero unexpected
  errors) after the fix, including the deliberately-thrown 422 from the invalid-cron test.
- Docker checks: verified live against a real Docker daemon (not a mock) through the running API — created
  a real container and a `docker`-kind check against it and watched it come back `up · running`; stopped
  the container and re-ran the check to see it flip to `down · status: exited`; ran a check against a
  container name that doesn't exist (`down · container not found`); created a container with a real
  `HEALTHCHECK` and an `expect=healthy` check and watched it report `warn · health check starting` right
  after creation, then flip to `up · healthy` once Docker's own health probe actually passed; confirmed
  `expect=healthy` against a container with **no** `HEALTHCHECK` at all correctly reports
  `down · no healthcheck defined for this container` rather than guessing. Also confirmed the socket-missing
  path reports a clear error rather than a raw traceback. Not tried: a container that goes `unhealthy` after
  previously being `healthy` (the "running, but unhealthy" `warn` branch is unit-tested, not exercised live).
- Docker checks in the UI (frontend, this session): `ServiceCheckModal.tsx` now has a fourth "Docker" option
  in its `SegmentedControl` (`CheckKind` in `lib/api.ts` gained `"docker"`), with the target field labeled
  "Container name or ID" and `expect` rendered as a **Running / Healthy** dropdown rather than free text —
  the backend only ever accepts `""`/`"running"`/`"healthy"` for this kind, so a free-text field (fine for
  http/tcp's numeric expectations) would just invite typos here. Verified live end to end through the
  actual rebuilt frontend container (not just the API): created a check via the dialog against a real
  container and watched it read `Up · running`; stopped the container and hit Refresh, saw it flip to
  `Down · status: exited`; added a second check with "Healthy" selected against a container with no
  `HEALTHCHECK`, saw `Down · no healthcheck defined for this container` — all matching the backend exactly,
  zero console errors.
  **Found and fixed a real deployment gap in the process** (an infra/docs gap, not a code bug): following
  the exact commented-out recipe in `docker-compose.yml` — mount the socket, nothing else — fails with
  `permission denied on /var/run/docker.sock` on a stock Docker Desktop setup, because `api`/`worker` run
  as uid:gid 1000:1000 while the socket there is owned `root:root` mode 660, so a non-root, non-matching-group
  process can't read it. Reproduced the failure live first, then fixed it by adding `group_add: ["0"]` to
  both services in the same commented example (root is a supplementary group here since the socket's group
  owner is root; on native Linux, where the socket is usually `root:docker` instead, the fix is the same
  shape but needs that group's real GID in place of `"0"`) — re-verified live after the fix and all three
  scenarios above came back correct. This also exercises the socket permission-denied path for real, which
  the backend's own step 9 verification had only reached as a documented-but-unexercised code path.
- Connection editing (frontend, this session): verified live through the rebuilt frontend container against
  one connection of each form kind. File-kind (SQLite): opened Edit, confirmed the path field pre-filled
  correctly, clicked Cancel and confirmed nothing changed server-side, then did a real edit (renamed it) and
  confirmed the path was untouched. Host-kind (PostgreSQL, pointed at the stack's own `postgres` container):
  confirmed host/port/database/user all pre-filled and the password field started blank with a "blank keeps
  the current password" hint, saved with the password left blank, then clicked **Test** and got back
  `up` — proving the encrypted password actually survived the edit rather than being wiped by the merge.
  URI-kind (MongoDB, no real server available — see the MongoDB caveat above): confirmed the connection
  string started blank with its own hint and the other fields pre-filled, edited an unrelated field, and
  saved successfully; a blank `uri` would have failed backend validation (`MongoConnector.validate()`
  requires it), so a clean save is itself proof the stored URI merged back in correctly, and the
  connection's masked `display_host` came back unchanged afterward. Zero console errors across all three.
  **Found and fixed a real render-order bug while building this**, not present in the create flow: resolving
  the fixed type and pre-filling the form from the saved config ran in a plain `useEffect`, which React runs
  after the browser paints — so the edit dialog would briefly flash an empty body before its fields appeared
  (worse right after the dialog's own `dbTypes()` fetch resolved, but reproducible even with an already-warm
  types list). Fixed by switching that effect to `useLayoutEffect` (resolves before paint) and by having
  `Connections.tsx` pass down the type list it already loads for its own table, instead of the dialog
  re-fetching its own copy on every open. Confirmed the flash is gone by screenshotting the same dialog open
  repeatedly.
- The design-system screen is the one remaining unbuilt placeholder (see Frontend, above).
