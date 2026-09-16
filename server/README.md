# server/ — technical reference

Shared gateway in front of ALL classification providers. User-facing docs
(EN/RU): root `README.md`, sections "Shared gateway" / «Общий шлюз».

## Lite gateway

> Current behavior. Where later sections of this file describe the gateway
> importing `service/pipeline.py` in-process, the Redis per-provider rate
> limiter (`ratelimit.py`), `RATE_LIMIT_*` settings, or publishing nginx on
> port 80, this section supersedes them.

The gateway does **caching and metrics only**; model work always happens
upstream over HTTP.

| concern | where it lives now |
|---|---|
| pHash cache | `cache.py` — async Redis, multi-index Hamming lookup (below) |
| metrics | `metrics.py` — SQLite, batched background writer thread per process |
| providers | `providers.py` — one pooled `httpx.AsyncClient`; daemon2 = `SERVICE_URL`, worker = `WORKER_URL`, BYO-key APIs |
| rate limiting | not in the app: nginx `limit_req` (edge), `service/ratelimit.py`, the worker's Durable Object |
| client IP | uvicorn `--proxy-headers` + `FORWARDED_ALLOW_IPS`; nginx overwrites `X-Forwarded-For` |

### API changes (extension contract unchanged)

- `/classify` and `/correct` keep their request/response bodies and the
  `X-Cache` / `X-Phash` headers (now listed in
  `Access-Control-Expose-Headers`).
- Upstream failures are real HTTP statuses instead of a fake
  `{"isMeme": false}`: `429` (`{"error": "rate_limited",
  "retry_after_seconds": N}` + `Retry-After`), `502` (upstream error or bad
  key), `503` (`provider_unavailable`, e.g. daemon2 without `SERVICE_URL`),
  `504` (upstream timeout). Nothing is cached on failure.
- A request without `provider` uses `DEFAULT_PROVIDER` (`worker`).
- `/health` reports `status: ok|degraded`, Redis reachability and which
  providers are configured; it no longer exposes file paths.
- `/docs` is off unless `DOCS_ENABLED=1`.
- `service/` `/classify` accepts an optional `rejectSlug` and returns
  per-stage timings as `X-OCR-Seconds`, `X-VLM-Seconds`, `X-OCR-Used`,
  `X-VLM-Ran`, `X-Pipeline-Path` headers (body unchanged), so corrections
  and the OCR/VLM metrics work over HTTP.

### Cache lookup

A 64-bit pHash is split into `PHASH_HAMMING_THRESHOLD + 1` disjoint
segments, each with a Redis SET of the hashes sharing that segment value.
Hashes within the threshold must share at least one segment exactly
(pigeonhole), so a lookup unions those sets and measures distance on the
candidates only — no false negatives, ~14x fewer comparisons at threshold 8
(far fewer at lower thresholds). An exact `GET` runs first. Keys:
`{CACHE_KEY_PREFIX}e:{phash}` (entry, per-entry TTL) and
`{CACHE_KEY_PREFIX}s{m}:{i}:{value}` (index). Stale index members are
pruned lazily. Changing the threshold starts a fresh index namespace
(existing entries stay reachable by exact match).

Results that daemon2 produced in **manual** mode (isMeme assumed, not
decided) are cached as slug-only: they serve manual requests, but never an
isMeme verdict to auto requests.

The old single-hash index (`dnm:cache:index`) from the previous gateway is
not read; delete it with `redis-cli DEL dnm:cache:index` if it exists.

### Deploying next to other applications

- Compose project name `daemon-names-memes` (override with
  `COMPOSE_PROJECT_NAME`), so containers/volumes/networks don't collide
  with other stacks started from a directory named `server`.
- nginx is published on `${GATEWAY_BIND:-127.0.0.1}:${GATEWAY_PORT:-8090}`.
  Behind an existing host proxy (nginx/Caddy/Traefik), proxy to that port
  and set `NGINX_TRUSTED_PROXY` to the proxy's source CIDR so the real
  client IP is used. To publish directly, set `GATEWAY_BIND=0.0.0.0`.
- nginx serves only `/classify`, `/correct`, `/health`; everything else is
  `404`.
- Redis runs with `maxmemory` (`REDIS_MAXMEMORY`, default `256mb`) and
  `allkeys-lru`. To use a shared external Redis instead, set `REDIS_URL`
  (own DB number recommended); all keys are under `CACHE_KEY_PREFIX`.
- `.env` is optional; see `.env.example` for every setting.

### daemon2

```bash
SERVICE_URL=http://service:8080 docker compose --profile daemon2 up -d --build
```

The `service` container is not published and runs with
`RATE_LIMIT_TRUST_PROXY=1`: the gateway forwards the real client IP, so
service/'s per-IP limit applies per user rather than to the gateway as a
whole. Any other service/ deployment works too — point `SERVICE_URL` at it
(only enable `RATE_LIMIT_TRUST_PROXY` there if it's not publicly reachable).

### Running without Docker

```bash
cd server && pip install -r requirements.txt
REDIS_URL=redis://127.0.0.1:6379/0 METRICS_DB_PATH=./data/metrics.sqlite3 \
  SERVICE_URL=http://127.0.0.1:8080 python app.py          # binds 127.0.0.1:8090
# tests: no Redis, no models, no network (fakeredis + mocked upstreams)
for t in _test_*.py; do python "$t"; done                  # needs fakeredis
```

## What this is

`server/` is a FastAPI gateway that every provider routes through (by
default — see extension/background.js `OWNER_GATEWAY_URL`; users can
override with their own instance via Settings → Gateway URL). It adds three
cross-provider services on top of the existing classification paths:

1. **Perceptual-hash cache (Redis)** — actively gates requests: a hit skips
   the model call entirely and returns the cached `{isMeme, filenameSlug}`.
2. **Per-provider rate limiting (Redis)** — INCR+EXPIRE fixed window.
   Keyless providers (`daemon2`, `worker`) are limited (default 5/min,
   hard ceiling). BYO-key providers (`google`, `claude`, `openai`,
   `openrouter`, `groq`, `mistral`, `xai`) are **exempt** — the user's own
   key means the user's own cost — but their results still populate the
   shared cache and metrics log.
3. **Metrics log (SQLite)** — append-only `classification_log` table: every
   request, hit or miss, with per-stage latency, cache Hamming distance,
   correction bookkeeping, and a nullable `manual_override_is_meme` for
   hand-labeled accuracy tracking.

Plus the **`/correct` endpoint** powering the extension's "Rename last"
flow, and **provider dispatch** (`providers.py`) so one gateway serves
every backend behind an identical detail-dict interface.

## Files

| file | purpose |
|---|---|
| `app.py` | FastAPI gateway: `/classify`, `/correct`, `/health`; cache gate → rate limit → provider dispatch → cache store → metrics, uniform for every provider |
| `providers.py` | dispatch table: `daemon2` (in-process `service/pipeline.py`), `worker` (HTTP proxy to the Cloudflare Worker, forwards `rejectSlug`), BYO-key providers (proxies Google/Claude/OpenAI-compatible APIs with the user's key) |
| `cache.py` | pHash cache (imagehash, Redis hash index, linear Hamming scan — see below) |
| `ratelimit.py` | Redis INCR+EXPIRE per provider+client; fail-open on Redis errors |
| `metrics.py` | SQLite schema + append-only writer; never raises into the request path |
| `config.py` | single env-driven config layer (same convention as service/) |
| `analyze_metrics.py` | CLI report over `classification_log` |
| `Dockerfile` | gateway image (bundles service/ code for the in-process daemon2 path) |
| `docker-compose.yml` | app + redis + nginx, one command |
| `nginx.conf` | coarse IP-based `limit_req` edge layer in front of the app limiter |
| `_test_*.py` | unit + E2E tests (fakeredis, stubbed pipeline — no models needed) |

## API

### POST /classify

```json
{
  "image": "<base64>", "mimeType": "image/png", "locale": "ru",
  "mode": "manual|auto",
  "provider": "daemon2|worker|google|claude|openai|openrouter|groq|mistral|xai",
  "apiKey": "<only for BYO-key providers — forwarded, never stored>"
}
```

Response: `{"isMeme": bool, "filenameSlug": str}` (+ `error` on failure).
Headers: `X-Cache: HIT|MISS`, `X-Phash: <16-hex>`, `X-RateLimit-Remaining`
(keyless providers). Over limit: HTTP 429 with
`{"error": "rate_limited", "retry_after_seconds": N}` — same JSON contract,
CORS `*` on every path.

### POST /correct — "Rename last"

```json
{
  "image": "<base64 of the ORIGINAL image>", "mimeType", "locale", "mode",
  "provider", "apiKey",
  "phash": "<X-Phash from the flagged result>",
  "cache_hit": true,
  "previous_slug": "<the slug the user rejected>"
}
```

Always forces a fresh model pass with `previous_slug` injected as an
explicit negative example ("do not repeat the previous answer or a close
variant"). Then:

- **Scenario A** (`cache_hit: true`): the cache entry for `phash` is
  **overwritten** with the corrected result, so future near-duplicate
  images get the corrected answer instead of repeating the mistake.
- **Scenario B** (`cache_hit: false`): cache untouched (the wrong result
  was never cached; a correction is one user's judgment, not automatically
  everyone's cached answer).

Both scenarios log `was_correction=1`, `previous_wrong_slug`, and
`correction_was_cache_hit` (0/1) so correction frequency is trackable per
scenario — a high rate on cache hits means `PHASH_HAMMING_THRESHOLD` is too
loose; a high rate on fresh calls means the prompt/model needs work.

**Extension-side limitation (documented, by design):** Chrome extensions
cannot rename files on disk after download. "Rename last" re-downloads the
same source image under the corrected name; the old file stays. The popup
says so, and the flow is chainable (a second click corrects the latest
attempt, not the original).

### GET /health

Redis status, cache size (+ scan-size warning), rate-limit config, metrics
DB path.

## Perceptual-hash cache — design choice

pHash = `imagehash.phash` (64-bit, hash_size 8). Two images within
**`PHASH_HAMMING_THRESHOLD` (default 8, named constant)** bits are "the same
meme".

Redis has no native Hamming-distance query. Options were (a) Redis-side
BITCOUNT-on-XOR with per-bit SETBIT scaffolding, or (b) one Redis hash as
the index, HGETALL + Python-side `int XOR .bit_count()`. **Chose (b)**: at
pet-project scale (thousands–tens-of-thousands of entries; a 64-bit hash is
8 bytes, so even 50k entries is a few hundred KB) the fetch+scan is
sub-millisecond and vastly simpler. `/health` warns above
`CACHE_SCAN_WARN_SIZE` (default 50k) — past that, move to (a) or a proper
BK-tree/vector index; `cache.py` is isolated so the swap is contained.

No TTL by default (`CACHE_TTL_S=0`) — memes don't go stale. Cache is
**provider-agnostic by design**: an image cached via `google` is served to
a `daemon2` or `claude` request (the `{isMeme, filenameSlug}` contract is
identical across providers — that sharing is the point of the gateway).

Fail-open everywhere: Redis down → cache reads miss, limiter allows, the
request still completes via the provider.

## Rate limiting — division of labor

1. **nginx `limit_req`** (30r/m per IP + burst 10, in `nginx.conf`): coarse,
   cheap, stops floods before they reach Python.
2. **Redis per-provider counters** (`ratelimit.py`): the actual business
   rule — 5/min hard ceiling for keyless providers, enforced server-side so
   the client can't raise it. BYO-key traffic skips this layer entirely.

The Cloudflare Worker additionally keeps its own Durable-Object 5/min limit
(two independent layers when routed via the gateway — intentional: the
worker must stay safe for direct callers too).

State is process-local to Redis: fine for one gateway instance; multiple
replicas already share correctly because the counters live in Redis (not in
the app process).

## Redis deployment

**Chosen: same-host docker-compose (`redis:7-alpine` sidecar)** — see
`docker-compose.yml`. Why: the whole point of this gateway is a shared
cache across users, which requires ONE reachable Redis; a sidecar in the
same compose file is the cheapest zero-external-dependency way to get that
on any Docker host (VPS, Fly volumes, etc.), with AOF persistence
(`--appendonly yes`) so the cache survives restarts. Managed Redis
(Upstash/Redis Cloud free tiers) is the alternative if you deploy the app
on a platform without volumes/persistent disks — point `REDIS_URL` at it
and delete the `redis:` service; no code changes (the app only ever reads
`REDIS_URL`).

## Metrics (SQLite — deliberately not Redis)

`classification_log` is a write-once append log, not a high-concurrency
lookup store — Redis would add an operational dependency for zero benefit,
so it stays SQLite (WAL mode, `synchronous=NORMAL`, one connection per
write, writer failures swallowed + logged so metrics can never break a
response). Schema highlights: per-stage latencies (`ocr_seconds`,
`vlm_seconds`, `total_seconds`), `cache_hit` + `cache_hamming_distance`,
`provider`, `mode`, `was_correction`, `previous_wrong_slug`,
`correction_was_cache_hit`, nullable `manual_override_is_meme`.

### analyze_metrics.py

```bash
python analyze_metrics.py --db /data/metrics.sqlite3 [--days 7]
```

Reports: cache hit rate + Hamming histogram of actual hits; OCR→VLM
fallback rate (manual mode); p50/p95/p99 latency per stage; false
negative/positive rates over hand-labeled rows; correction rate overall
and split by scenario A (cache hit) vs B (fresh call), with per-scenario
denominators and what each imbalance means.

## Running

```bash
# full stack (gateway + redis + nginx) — build context is the repo root
cd server && docker compose up --build
# gateway alone for development (needs a Redis on localhost:6379 and
# service/ deps installed; models download on first use)
cd server && PORT=8090 ../service/.venv/bin/python app.py
# tests (no Redis server, no models — fakeredis + stubs)
cd server && for t in _test_*.py; do ../service/.venv/bin/python $t; done
```

Config: copy `.env.example` to `.env` (compose picks it up) or set env vars
directly. Notable: `WORKER_URL`, `RATE_LIMIT_*_PER_WINDOW`,
`PHASH_HAMMING_THRESHOLD`, `REDIS_URL`, `METRICS_DB_PATH`,
`RATE_LIMIT_TRUST_PROXY` (default 1 because nginx is the intended front
door — set 0 if exposing the app directly).

## Split deployment note

Default is in-process: the gateway imports `service/pipeline.py` directly
(`_import_service_pipeline()` isolates the colliding `config`/`ocr` module
names — do not "simplify" it to a bare sys.path insert, see the comment in
app.py). Set `SERVICE_URL` to proxy to a remote `service/` instead; that
path currently doesn't forward `reject_slug` (service/app.py's `/classify`
doesn't accept it) — extend it there if you need corrections in a split
deployment.
