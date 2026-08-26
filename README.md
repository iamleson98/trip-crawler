# vexerers

A high-performance, reverse-engineered Rust client + crawler for the public
**VeXeRe** (`vexere.com`) bus-trip APIs. Anonymous-only — no booking, no
payment, no authed endpoints.

> VeXeRe is the largest online bus-ticketing platform in Vietnam. This crate
> targets the same microservices the Next.js frontend calls internally; no
> private/protected endpoints are touched.

---

## Highlights

- **Library + binary** — `vexerers` (lib) for embedding, `vexere` (bin) for CLI.
- **64-way concurrency** out of the box (Aggressive profile) with bounded
  semaphore + exponential backoff on 429 / 5xx.
- **Parallel pagination** — `search_all_pages` fetches page 1 to learn
  `total_pages`, then pages 2..N concurrently.
- **Type-tolerant serde** — VeXeRe's JSON field types drift across records
  (a field is `string` here, `int` there, `null` elsewhere). The `FlexString`
  newtype + `Option<f64>` score fields swallow that drift without panicking.
- **Crash-safe JSONL sink** — every crawled trip is line-buffered and flushed
  to disk per write, so a Ctrl-C never loses already-fetched rows.
- **No runtime auth flow** — `POST /getToken` returns an anonymous JWT
  (`{typ:2, usr:"fe", ...}`) which is cached + auto-refreshed on expiry.

---

## Quick start

```bash
# build
cargo build --release

# one-shot trip search (page 1, pretty JSON)
./target/release/vexere search --from 24 --to 482 --date 2026-09-01 --pretty

# 7-day trip crawl → /home/z/my-project/download/trips-out/trips.jsonl
./target/release/vexere crawl-trips \
    --from 24 --to 482 \
    --from-date 2026-09-01 --to-date 2026-09-07 \
    --out ./trips-out

# area autocomplete
./target/release/vexere areas -q "Hà Nội"

# area detail by ID
./target/release/vexere area 24 --pretty

# bus / flight / train counts for a route + date
./target/release/vexere counts --kind bus --from 24 --to 482 --date 2026-09-01

# full multi-route crawl across a date range → ./vexere-out/{trips,areas}.jsonl
./target/release/vexere crawl-all --from-date 2026-09-01 --to-date 2026-09-07 --out ./vexere-out
```

### CLI global flags

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | `aggressive` | `polite` (8) / `moderate` (32) / `aggressive` (64) |
| `--concurrency N` | (profile) | Override max in-flight requests |
| `--jitter MS` | `0` | Per-request jitter before send |
| `--timeout SEC` | `30` | Per-request HTTP timeout |
| `-v` / `--verbose` | off | Log every request via `tracing` |

`RUST_LOG=vexerers=trace` turns on full request logging.

---

## Library usage

```rust,no_run
use vexerers::{Config, VexereClient};

let client = VexereClient::new(Config::default())?;

// one-shot: page 1 of search results
let resp = client.trips().search(24, 482, "2026-09-01").await?;
println!("page {}/{}: {} trips", resp.page, resp.total_pages, resp.data.len());

// all pages, fetched in parallel (bounded by the client's 64-way semaphore)
use vexerers::api::trips::TripFilter;
let all_trips: Vec<_> = client
    .trips()
    .search_all_pages(TripFilter::new(24, 482, "2026-09-01"))
    .await?;

// area autocomplete
let areas = client.areas().search("Hà Nội").await?;

// crash-safe JSONL sink
use vexerers::sink::JsonlSinkDir;
let sink = JsonlSinkDir::new("./out");
sink.write_many("trips", &all_trips).await?;
```

---

## Architecture

```
vexerers (lib)                      vexere (bin)
├── client.rs    ─ VexereClient      └── src/bin/vexere.rs ─ clap CLI
│   • bearer token cache                ├─ search
│   • 64-way semaphore                  ├─ crawl-trips
│   • exp backoff + jitter              ├─ crawl-all
├── config.rs    ─ Config + Endpoints  ├─ areas / area
├── api/         ─ one module / surface├─ counts
│   ├── auth.rs  ─ POST /getToken       └─ home
│   ├── area.rs  ─ v3/area + goyolo
│   ├── trips.rs ─ v2/route (★)
│   ├── counts.rs─ bus/flight/train counts
│   └── home.rs  ─ /vi-VN/Home/* HTML fragments
├── models/      ─ serde types + FlexString
├── sink.rs      ─ JsonlSink / JsonlSinkDir
└── error.rs     ─ Error, retryable classifier
```

### Microservices (reverse-engineered from `window.__NEXT_DATA__.runtimeConfig.URL`)

| Host | Role | Used? |
|------|------|-------|
| `vexere.com` | Next.js frontend + ASP.NET MVC | ✅ `/getToken`, `/vi-VN/Home/*` |
| `internal-vroute.vexere.com` | **Bus trip search API** | ✅ `v2/route/`, `v3/area`, `v1/goyolo/area/{id}`, `v2/route/{kind}/count` |
| `api.vexere.com` | Main gateway (mostly authed) | ❌ |
| `vgate.vexere.com` | Payment gateway (authed) | ❌ skipped |
| `trip-service.vexere.com` | Booking (authed) | ❌ skipped |
| `feconfig.vexere.com`, `seo-service.vexere.com`, `vconfiguration.vexere.com`, `vmaps.vexere.com`, `vtracking.vexere.com`, `account-service.vexere.com`, `user-profile-service.vexere.com` | various | ❌ |

### Anonymous auth flow

```
POST https://vexere.com/getToken    (empty body)
→ { access_token: "<JWT>", refresh_token: "...", expires_in: 3600 }
```

The JWT payload decodes to `{typ:2, usr:"fe", cid:"a4eac500-3624-11e5-ac9e-09124c601013", exp:...}` — an anonymous frontend-internal token valid for 1 hour. The client caches it and re-fetches on expiry.

### Required request headers (sent on every internal-API call)

```
Authorization:        bearer <jwt>
Accept-Language:      vi-VN
origin-request-product: vex-web
origin-request-id:    vex-web_<ms>_<6char>
Origin:               https://vexere.com
Referer:              https://vexere.com/
```

### Bus trip search — the canonical endpoint

```
GET https://internal-vroute.vexere.com/v2/route/
    ?filter[from]=24
    &filter[to]=482
    &filter[quantity]=1
    &filter[date]=2026-09-01
    &page=1
    &pagesize=50
    &sort=asc
    &is_trigger_crawl=1
```

`is_trigger_crawl=1` hints the server to refresh live seat availability
before responding — same flag the production frontend sends.

---

## Concurrency & retry model

- `tokio::sync::Semaphore` with `64` permits (default Aggressive profile).
- Each request acquires one permit for its full lifecycle (header build →
  response decode).
- On `429 Too Many Requests` or `5xx`, exponential backoff with full-jitter:
  `delay = min(backoff_max, backoff_base * 2^attempt) * rand(0..1)`.
- Hard cap of `max_retries = 5` and `call_deadline = 120 s` per logical call.

---

## Type-tolerance: VeXeRe's drift problem

VeXeRe's JSON responses are produced by multiple microservices whose schemas
have drifted over time. The same logical field can appear as `string`,
`number`, `null`, or be absent across different records in the same response.

`vexerers` handles this with a few serde tricks:

| Field pattern | Solution |
|---|---|
| `parent_id`, `country_id`, `state_id`, `area_id`, `x_migrate_key`, `duplicated_parent_id` | `FlexString` newtype — deserializes from `string \| number \| null` into a normalized `String` |
| `is_deal_1111`, `speaking_english_utility` (sometimes int, sometimes bool) | typed as `bool` |
| `discount_score`, `coupon_score` (sometimes int, sometimes float) | typed as `f64` |
| `commission_score_normalization`, `penalty_score_normalization` (sometimes int, sometimes float, sometimes null) | typed as `Option<f64>` |

See `src/models/area.rs` and `src/models/trip.rs` for the `FlexString`
implementation — it's a 30-line newtype that's worth stealing for any
real-world Vietnamese SaaS API.

---

## Sample crawl output

A real 7-day crawl of Hà Nội (24) → Sa Pa (482), 2026-09-01 → 2026-09-07:

```
==> crawling 24->482 on 2026-09-01
    got 323 trips across all pages
==> crawling 24->482 on 2026-09-02
    got 320 trips across all pages
==> crawling 24->482 on 2026-09-03
    got 325 trips across all pages
==> crawling 24->482 on 2026-09-04
    got 323 trips across all pages
==> crawling 24->482 on 2026-09-05
    got 288 trips across all pages
==> crawling 24->482 on 2026-09-06
    got 287 trips across all pages
==> crawling 24->482 on 2026-09-07
    got 286 trips across all pages
==> done; wrote 2152 trips to ./trips-out
```

Output is one trip per line in `trips.jsonl` (NDJSON), each line a
self-contained `Trip` object:

```json
{"idIndex":"401932250_2026-09-01_23:00_24_38","route":{"id":34608615,"name":" Bắc Giang-Sapa ","pickup_points":[{"id":2966,"duration":-10,"district":"Hà Nội","name":"Sân Bay Nội Bài, ...","location":{"lat":21.214475,"lon":105.80241},...}],...}}
```

---

## Fixtures

`fixtures/` holds anonymized real API response samples:

- `token.json` — `POST /getToken` response shape
- `area_hanoi.json` — `GET /v1/goyolo/area/24`
- `trips_hanoi_to_sapa.json` — `GET /v2/route/?filter[from]=24&filter[to]=482&...`

These feed the integration tests in `tests/`.

---

## Development

```bash
# format + lint
cargo fmt --all
cargo clippy --all-targets -- -D warnings

# tests (wiremock — no real network)
cargo test --all

# build release
cargo build --release
```

The integration tests use [`wiremock`](https://docs.rs/wiremock) to spin up a
local mock server, so they don't hit VeXeRe's production API — safe to run
offline and in CI.

---

## Legal & ethical use

- This crate issues only **anonymous, public** API calls — the same ones a
  browser makes when loading `vexere.com`.
- **No** authed endpoints (booking, payment, account) are touched.
- Default concurrency (64) and the `is_trigger_crawl=1` flag match what the
  production frontend already sends — no extra load beyond what a normal user
  session generates.
- Respect VeXeRe's `robots.txt` and Terms of Service. Don't resell the data
  without permission. Crawl responsibly.

---

## License

Dual-licensed under MIT OR Apache-2.0, matching Rust ecosystem conventions.
