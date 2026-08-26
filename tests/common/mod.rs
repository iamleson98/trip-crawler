//! Shared test helpers: client construction with mock endpoints, and
//! canned wiremock stubs for the canonical endpoints.
//!
//! Some helpers may be unused in a given test binary (e.g. `stub_trip_search`
//! in tests that only exercise the extract module). Suppress the dead_code
//! warning to keep the public surface stable across test files.

#![allow(dead_code)]

use url::Url;
use wiremock::{Mock, MockServer, ResponseTemplate};

use vexerers::{Config, Endpoints, VexereClient};

/// Build a [`VexereClient`] whose `Endpoints.base` and `Endpoints.trip_service`
/// both point at the given [`MockServer`]. No real network calls will leave
/// the test process.
pub fn client_for(server: &MockServer) -> VexereClient {
    let uri = server.uri();
    let parsed = Url::parse(&uri).unwrap();
    let endpoints = Endpoints {
        base: parsed.clone(),
        trip_service: parsed,
        ..Endpoints::default()
    };
    let cfg = Config::builder()
        .endpoints(endpoints)
        .max_retries(5)
        .build();
    VexereClient::new(cfg).expect("client builds")
}

/// Stub `POST /getToken` returning a fixed anonymous JWT, up to `n` times.
pub async fn stub_get_token(server: &MockServer, n: u64) {
    Mock::given(wiremock::matchers::method("POST"))
        .and(wiremock::matchers::path("/getToken"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "token_type": "bearer",
            "access_token": "test-anon-jwt",
            "refresh_token": "test-refresh",
            "expires_in": 3600_u32,
        })))
        .up_to_n_times(n)
        .mount(server)
        .await;
}

/// Stub `GET /v3/area?q=...` with a 4-row sample. Hit up to `n` times.
pub async fn stub_area_search(server: &MockServer, n: u64) {
    let body = serde_json::json!({
        "message": "success",
        "data": [
            {
                "id": "24",
                "name": "Hà Nội",
                "code": "HNI",
                "parent_id": "0",
                "country_id": "1",
                "state_id": null,
                "search_type": 1,
                "type": 1,
                "is_popular": 1,
                "lat": 21.0285,
                "lon": 105.8542
            },
            {
                "id": "38",
                "name": "Lào Cai",
                "code": "LCA",
                "parent_id": 0,
                "country_id": 1,
                "state_id": "32",
                "search_type": 1,
                "type": 1,
                "is_popular": 0,
                "lat": 22.3388,
                "lon": 103.9192
            },
            {
                "id": "482",
                "name": "Sa Pa",
                "code": "SAPA",
                "parent_id": "38",
                "country_id": "1",
                "state_id": null,
                "search_type": 1,
                "type": 1,
                "is_popular": 1,
                "lat": 22.3368,
                "lon": 103.8439
            },
            {
                "id": "501095",
                "name": "Bến xe Mỹ Đình",
                "code": "MYDINH",
                "parent_id": "24",
                "country_id": "1",
                "state_id": null,
                "search_type": 2,
                "type": 2,
                "is_popular": 0,
                "lat": 21.0289,
                "lon": 105.7778
            }
        ],
        "page": 1,
        "page_size": 50,
        "total": 4,
        "total_pages": 1
    });
    Mock::given(wiremock::matchers::method("GET"))
        .and(wiremock::matchers::path("/v3/area"))
        .and(wiremock::matchers::query_param("q", "Hà Nội"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .up_to_n_times(n)
        .mount(server)
        .await;
}

/// Stub `GET /v1/goyolo/area/24` returning the Hà Nội area detail.
pub async fn stub_area_detail(server: &MockServer, n: u64) {
    let body = serde_json::json!({
        "message": "success",
        "data": {
            "id": "24",
            "name": "Hà Nội",
            "code": "HNI",
            "parent_id": "0",
            "country_id": "1",
            "state_id": null,
            "x_migrate_key": 24,
            "duplicated_parent_id": null,
            "type": 3,
            "sub_level": 1,
            "latitude": 21.0285,
            "longitude": 105.8542,
            "is_search_on_fe": 1
        }
    });
    Mock::given(wiremock::matchers::method("GET"))
        .and(wiremock::matchers::path("/v1/goyolo/area/24"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .up_to_n_times(n)
        .mount(server)
        .await;
}

/// Stub `GET /v2/route/?...` returning a single-trip sample (page 1 of 1).
pub async fn stub_trip_search(server: &MockServer, n: u64) {
    let body = page_body(1, 1, "24", "38");
    Mock::given(wiremock::matchers::method("GET"))
        .and(wiremock::matchers::path("/v2/route/"))
        .and(wiremock::matchers::query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(body))
        .up_to_n_times(n)
        .mount(server)
        .await;
}

/// Build a one-trip `TripSearchResponse` JSON value with `total_pages` set
/// to `total_pages`, the trip tagged with the given `from` and `to` substrings.
#[allow(dead_code)]
pub fn page_body(page: u32, total_pages: u32, from: &str, to: &str) -> serde_json::Value {
    let id_index = format!("351932250_2026-09-01_23:00_{from}_{to}");
    serde_json::json!({
        "message": "success",
        "data": [{
            "idIndex": id_index,
            "route": {
                "id": 34608615,
                "name": " Bắc Giang-Sapa ",
                "pickup_points": [{
                    "id": 2966,
                    "duration": -10,
                    "name": "Sân Bay Nội Bài",
                    "district": "Hà Nội",
                    "location": {"lat": 21.214475, "lon": 105.80241},
                    "type": 1,
                    "area_id": "24"
                }],
                "dropoff_points": [{
                    "id": 2403,
                    "duration": 265,
                    "name": "Văn phòng Bảo Hà",
                    "district": "Lào Cai",
                    "location": {"lat": 22.1515, "lon": 104.344467},
                    "type": 1,
                    "area_id": "38"
                }],
                "schedules": [{
                    "trip_code": "abc123",
                    "hour": "23",
                    "minute": "00",
                    "pickup_date": "2026-09-01T23:00:00+07:00",
                    "arrival_time": "2026-09-02T05:00:00+07:00",
                    "fare": {
                        "original": 350000,
                        "discount": 50000,
                        "selling": 300000
                    },
                    "available_seats": 12,
                    "total_seats": 24,
                    "vehicle_type": "Giường nằm 24 chỗ"
                }]
            },
            "company": {"id": 1, "name": "Test Operator"},
            "max_total_seats": 24,
            "status": 1,
            "is_sponsor": 0,
            "score": 0.5,
            "discount_score": 0.1,
            "coupon_score": 0.0,
            "is_deal_1111": false,
            "speaking_english_utility": false
        }],
        "page": page,
        "page_size": 50,
        "total": 1,
        "total_pages": total_pages,
        "area": {},
        "companies": [],
        "diversity": {}
    })
}
