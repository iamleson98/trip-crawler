//! Integration tests for `vexerers`.
//!
//! These tests use [`wiremock`] to spin up a local HTTP server that mocks the
//! VeXeRe microservices. No real network calls are made — safe for offline CI.

mod common;

use pretty_assertions::assert_eq;
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

use vexerers::api::trips::{TripFilter, TripSearchOptions};
use vexerers::models::area::FlexString;
use vexerers::sink::JsonlSinkDir;
use vexerers::{Config, Endpoints, Trip, TripSearchResponse};

// ---------------------------------------------------------------------------
// Unit-style tests for FlexString (no HTTP).
// ---------------------------------------------------------------------------

#[test]
fn flexstring_accepts_json_string() {
    #[derive(serde::Deserialize)]
    struct W {
        id: FlexString,
    }
    let w: W = serde_json::from_str(r#"{"id":"24"}"#).unwrap();
    assert_eq!(w.id.as_str(), Some("24"));
}

#[test]
fn flexstring_accepts_json_number() {
    #[derive(serde::Deserialize)]
    struct W {
        id: FlexString,
    }
    let w: W = serde_json::from_str(r#"{"id":24}"#).unwrap();
    assert_eq!(w.id.as_str(), Some("24"));
}

#[test]
fn flexstring_accepts_json_null() {
    #[derive(serde::Deserialize)]
    struct W {
        id: FlexString,
    }
    let w: W = serde_json::from_str(r#"{"id":null}"#).unwrap();
    assert_eq!(w.id.as_str(), None);
}

#[test]
fn flexstring_round_trips_through_serialize() {
    #[derive(serde::Deserialize, serde::Serialize)]
    struct W {
        id: FlexString,
    }
    let w: W = serde_json::from_str(r#"{"id":482}"#).unwrap();
    let s = serde_json::to_string(&w).unwrap();
    assert_eq!(s, r#"{"id":"482"}"#);
}

// ---------------------------------------------------------------------------
// Token + client setup.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn token_is_fetched_on_first_internal_call() {
    let server = MockServer::start().await;
    // Allow up to 1 token fetch; if the client fetches more than once the
    // second call will see no matching mock and 404, failing the test.
    common::stub_get_token(&server, 1).await;
    common::stub_area_search(&server, 5).await;

    let client = common::client_for(&server);
    // First call: token is fetched + area search runs.
    let resp = client.areas().search("Hà Nội").await.unwrap();
    assert_eq!(resp.data.len(), 4);

    // Second call: token must come from cache (no second /getToken).
    let resp2 = client.areas().search("Hà Nội").await.unwrap();
    assert_eq!(resp2.data.len(), 4);
}

// ---------------------------------------------------------------------------
// Area autocomplete — GET /v3/area?q=...&is_merged_province=1
// ---------------------------------------------------------------------------

#[tokio::test]
async fn area_search_parses_typical_response() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;
    common::stub_area_search(&server, 1).await;

    let client = common::client_for(&server);
    let resp = client.areas().search("Hà Nội").await.unwrap();

    assert_eq!(resp.message, "success");
    assert_eq!(resp.data.len(), 4, "expected 4 area matches");
    // First match should be Hà Nội city.
    let first = resp.data.first().unwrap();
    assert_eq!(first.name, "Hà Nội");
    // parent_id can be a JSON string OR a number — both must parse via FlexString.
    assert!(first.parent_id.as_str().is_some());
}

// ---------------------------------------------------------------------------
// Area detail — GET /v1/goyolo/area/{id}
// ---------------------------------------------------------------------------

#[tokio::test]
async fn area_detail_parses_typical_response() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;
    common::stub_area_detail(&server, 1).await;

    let client = common::client_for(&server);
    let area = client.areas().detail(24).await.unwrap();

    assert_eq!(area.name, "Hà Nội");
    assert_eq!(area.code.as_deref(), Some("HNI"));
    assert_eq!(area.id, "24");
    // Lat/Lon come through as flat f64.
    let lat = area.latitude.expect("latitude present");
    let lon = area.longitude.expect("longitude present");
    assert!((lat - 21.0_f64).abs() < 1.0, "lat approx 21.x, got {lat}");
    assert!((lon - 105.0_f64).abs() < 1.0, "lon approx 105.x, got {lon}");
}

// ---------------------------------------------------------------------------
// Trip search (single page) — GET /v2/route/?filter[from]=...&...
// ---------------------------------------------------------------------------

#[tokio::test]
async fn trip_search_parses_typical_response() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;
    common::stub_trip_search(&server, 1).await;

    let client = common::client_for(&server);
    let resp = client.trips().search(24, 482, "2026-09-01").await.unwrap();

    assert_eq!(resp.message, "success");
    assert_eq!(resp.data.len(), 1, "one trip in the fixture");
    let trip = &resp.data[0];
    assert_eq!(trip.id_index, "351932250_2026-09-01_23:00_24_38");
    assert_eq!(trip.route.id, 34608615);
    assert!(!trip.route.pickup_points.is_empty(), "has pickup points");
    assert!(!trip.route.dropoff_points.is_empty(), "has dropoff points");
    // The fixture has one schedule with original=350000, discount=50000.
    let best = trip.best_fare_vnd();
    assert_eq!(best, Some(300_000));
}

// ---------------------------------------------------------------------------
// Trip search all pages — parallel pagination.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn trip_search_all_pages_concats_all_pages() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;

    // Page 1 returns total_pages=2 and one trip; page 2 returns a different trip.
    // wiremock matches by the `page` query param.
    Mock::given(method("GET"))
        .and(path("/v2/route/"))
        .and(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(common::page_body(1, 2, "24", "38")))
        .up_to_n_times(1)
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/v2/route/"))
        .and(query_param("page", "2"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(common::page_body(2, 2, "482", "1000")),
        )
        .up_to_n_times(1)
        .mount(&server)
        .await;

    let client = common::client_for(&server);
    let trips: Vec<Trip> = client
        .trips()
        .search_all_pages(TripFilter::new(24, 482, "2026-09-01"))
        .await
        .unwrap();

    assert_eq!(trips.len(), 2, "concatenated both pages");
    assert_eq!(trips[0].id_index, "351932250_2026-09-01_23:00_24_38");
    assert_eq!(trips[1].id_index, "351932250_2026-09-01_23:00_482_1000");
}

// ---------------------------------------------------------------------------
// Retry on 5xx — first attempt 503s, second attempt succeeds.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn trip_search_retries_on_503_then_succeeds() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;

    // First call: 503. Second call: 200 with valid response.
    Mock::given(method("GET"))
        .and(path("/v2/route/"))
        .respond_with(ResponseTemplate::new(503).set_body_string("Service Unavailable"))
        .up_to_n_times(1)
        .mount(&server)
        .await;

    common::stub_trip_search(&server, 1).await;

    // Speed up the test by using a tiny backoff. We don't have a public knob for
    // backoff_base on the builder, so we just accept the default 250 ms — the
    // total wall time is still well under the test timeout.
    let client = common::client_for(&server);
    let resp = client.trips().search(24, 482, "2026-09-01").await.unwrap();
    assert_eq!(resp.data.len(), 1, "eventually succeeded after retry");
}

// ---------------------------------------------------------------------------
// JSONL sink — write_many round-trips.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn jsonl_sink_write_many_round_trips() {
    let tmp = tempfile_dir();
    let sink = JsonlSinkDir::new(&tmp);

    // Construct trips via serde_json — `Trip`'s fields are all `#[serde(default)]`
    // so a partial JSON object round-trips into a fully-formed Trip.
    let trips: Vec<Trip> = (0..3)
        .map(|i| {
            serde_json::from_value(serde_json::json!({
                "idIndex": format!("trip-{i}")
            }))
            .expect("trip parses from partial json")
        })
        .collect();

    let written = sink.write_many("trips", &trips).await.unwrap();
    assert_eq!(written, 3);

    let content = std::fs::read_to_string(tmp.join("trips.jsonl")).unwrap();
    let lines: Vec<&str> = content.lines().filter(|l| !l.is_empty()).collect();
    assert_eq!(lines.len(), 3, "3 trips → 3 lines");
    for (i, line) in lines.iter().enumerate() {
        let v: serde_json::Value = serde_json::from_str(line).unwrap();
        assert_eq!(v["idIndex"], format!("trip-{i}"));
    }
}

#[tokio::test]
async fn jsonl_sink_dir_appends_across_writes() {
    let tmp = tempfile_dir();
    let sink = JsonlSinkDir::new(&tmp);

    let t1: Trip = serde_json::from_value(serde_json::json!({"idIndex": "t1"})).unwrap();
    let t2: Trip = serde_json::from_value(serde_json::json!({"idIndex": "t2"})).unwrap();

    sink.write("trips", &t1).await.unwrap();
    sink.write("trips", &t2).await.unwrap();

    let content = std::fs::read_to_string(tmp.join("trips.jsonl")).unwrap();
    assert_eq!(content.lines().filter(|l| !l.is_empty()).count(), 2);
}

// ---------------------------------------------------------------------------
// Header presence — bearer token + origin-request-product must be sent.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn internal_request_carries_required_headers() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;

    // Stub the area search with header matchers: only matches if the client
    // actually sent the bearer token + product headers.
    let body = serde_json::json!({
        "message": "success",
        "data": [],
        "page": 1, "page_size": 50, "total": 0, "total_pages": 1
    });
    Mock::given(method("GET"))
        .and(path("/v3/area"))
        .and(wiremock::matchers::header(
            "authorization",
            "bearer test-anon-jwt",
        ))
        .and(wiremock::matchers::header(
            "origin-request-product",
            "vex-web",
        ))
        .and(wiremock::matchers::header("accept-language", "vi-VN"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .up_to_n_times(1)
        .mount(&server)
        .await;

    let client = common::client_for(&server);
    // If the client didn't send the required headers, the mock wouldn't match,
    // the response would be 404 (MockServer default), and `.search` would error.
    let resp = client.areas().search("anything").await.unwrap();
    assert_eq!(resp.data.len(), 0);
}

// ---------------------------------------------------------------------------
// Search filter validation — bad date format is rejected early.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn trip_search_rejects_bad_date_format() {
    let server = MockServer::start().await;
    common::stub_get_token(&server, 1).await;

    let client = common::client_for(&server);
    // No mock for /v2/route/ — if the request actually fires, the test fails
    // with a connection error. We expect a local validation error instead.
    let res = client
        .trips()
        .search_with(
            TripFilter::new(24, 482, "2026/09/01"),
            TripSearchOptions::default(),
        )
        .await;
    assert!(res.is_err(), "bad date format should error before send");

    let err = res.unwrap_err().to_string();
    assert!(
        err.contains("date") || err.contains("YYYY-MM-DD"),
        "error should mention date format, got: {err}"
    );
}

// ---------------------------------------------------------------------------
// Helpers (small enough to live in this file too).
// ---------------------------------------------------------------------------

fn tempfile_dir() -> std::path::PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!(
        "vexerers-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

// ---------------------------------------------------------------------------
// Sanity: the example fixtures in `fixtures/` actually parse.
// (Guard against future schema drift breaking our deserializers.)
// ---------------------------------------------------------------------------

#[test]
fn fixture_token_parses() {
    let raw = include_str!("../fixtures/token.json");
    let v: serde_json::Value = serde_json::from_str(raw).unwrap();
    assert!(v.get("access_token").is_some(), "fixture has access_token");
}

#[test]
fn fixture_area_hanoi_parses_into_search_response() {
    // The fixture is the response from GET /v3/area?q=Hà Nội — a search
    // response (data is an array of AreaSearchItem), not a single-area detail.
    let raw = include_str!("../fixtures/area_hanoi.json");
    let r: vexerers::AreaSearchResponse = serde_json::from_str(raw).unwrap();
    assert!(!r.data.is_empty(), "fixture has at least one area hit");
    let first = r.data.first().unwrap();
    assert_eq!(first.name, "Hà Nội");
    assert_eq!(first.code.as_deref(), Some("HNI"));
    // parent_id can be a string or int — both must round-trip via FlexString.
    assert!(first.parent_id.as_str().is_some());
}

#[test]
fn fixture_trips_hanoi_to_sapa_parses_into_search_response() {
    let raw = include_str!("../fixtures/trips_hanoi_to_sapa.json");
    let r: TripSearchResponse = serde_json::from_str(raw).unwrap();
    assert!(!r.data.is_empty(), "fixture has at least one trip");
}

#[test]
fn config_builder_overrides_take_effect() {
    let cfg = Config::builder()
        .request_timeout(std::time::Duration::from_secs(99))
        .max_retries(2)
        .trace_requests(true)
        .build();
    assert_eq!(cfg.request_timeout, std::time::Duration::from_secs(99));
    assert_eq!(cfg.max_retries, 2);
    assert!(cfg.trace_requests);
}

#[test]
fn endpoints_default_points_at_production_hosts() {
    let e = Endpoints::default();
    assert_eq!(e.base.as_str(), "https://vexere.com/");
    assert_eq!(
        e.trip_service.as_str(),
        "https://internal-vroute.vexere.com/"
    );
}

// ---------------------------------------------------------------------------
// Hidden pickup-point filtering — the fix for the "1,953 pickup points" bug.
// ---------------------------------------------------------------------------

#[test]
fn fixture_trips_hidden_points_parses() {
    let raw = include_str!("../fixtures/trips_hidden_points.json");
    let r: TripSearchResponse = serde_json::from_str(raw).unwrap();
    assert_eq!(r.data.len(), 1, "fixture has one trip");
    let t = &r.data[0];
    assert_eq!(t.route.id, 7275428);
    // Raw (pre-filter) shape captured from the live API on 2026-09-09:
    // the route carries 2 hidden + 1 visible pickup, 1 hidden + 1 visible dropoff.
    assert_eq!(t.route.pickup_points.len(), 3);
    assert_eq!(t.route.dropoff_points.len(), 2);
}

#[test]
fn strip_hidden_points_keeps_only_visible_points() {
    let raw = include_str!("../fixtures/trips_hidden_points.json");
    let mut r: TripSearchResponse = serde_json::from_str(raw).unwrap();
    vexerers::api::trips::strip_hidden_points(&mut r);
    let t = &r.data[0];
    // Exactly the points the website renders for route 7275428.
    assert_eq!(t.route.pickup_points.len(), 1);
    assert_eq!(t.route.pickup_points[0].name, "Bến xe Thượng Lý");
    assert_eq!(t.route.pickup_points[0].hidden, 0);
    assert_eq!(t.route.dropoff_points.len(), 1);
    assert_eq!(t.route.dropoff_points[0].hidden, 0);
}

#[test]
fn strip_hidden_points_missing_field_treated_as_visible() {
    // Points without a `hidden` field at all must be kept (FE-tolerant).
    let raw = r#"{
        "message": "success",
        "data": [{
            "idIndex": "x",
            "route": {
                "id": 1,
                "pickup_points": [
                    {"id": 1, "name": "no hidden field"},
                    {"id": 2, "name": "hidden", "hidden": 1},
                    {"id": 3, "name": "explicitly visible", "hidden": 0}
                ],
                "dropoff_points": []
            },
            "company": {"id": 9, "name": "c"}
        }],
        "page": 1, "page_size": 50, "total": 1, "total_pages": 1
    }"#;
    let mut r: TripSearchResponse = serde_json::from_str(raw).unwrap();
    vexerers::api::trips::strip_hidden_points(&mut r);
    let pu = &r.data[0].route.pickup_points;
    assert_eq!(pu.len(), 2, "missing `hidden` kept + explicit visible kept");
    assert_eq!(pu[0].name, "no hidden field");
    assert_eq!(pu[1].name, "explicitly visible");
}
