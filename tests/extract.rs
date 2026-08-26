//! Integration tests for the `extract` module — brand deduplication, fare
//! flattening, and route summary aggregation from real trip records.

mod common;

use pretty_assertions::assert_eq;
use vexerers::{extract_brands, extract_fares, summarize_route, FareRow, Trip};

// ---------------------------------------------------------------------------
// Fixture parsing + extraction.
// ---------------------------------------------------------------------------

#[test]
fn extract_brands_dedupes_operators_across_trips() {
    let raw = include_str!("../fixtures/trips_hanoi_to_sapa.json");
    let resp: vexerers::TripSearchResponse = serde_json::from_str(raw).unwrap();
    assert!(!resp.data.is_empty(), "fixture has trips");

    let brands = extract_brands(&resp.data);
    // Each unique operator should appear exactly once.
    let mut ids: Vec<i64> = brands.iter().map(|b| b.id).collect();
    ids.sort_unstable();
    let n_unique = ids.len();
    let n_distinct = ids.iter().collect::<std::collections::BTreeSet<_>>().len();
    assert_eq!(n_unique, n_distinct, "brand IDs are unique after dedup");
    assert!(!brands.is_empty(), "at least one brand extracted");
    // Every brand should have a non-empty name (or id==0 placeholder, which
    // is filtered out by extract_brands).
    for b in &brands {
        assert!(
            !b.name.is_empty() || b.id == 0,
            "brand {:?} has empty name",
            b.id
        );
    }
}

#[test]
fn extract_fares_one_row_per_schedule() {
    let raw = include_str!("../fixtures/trips_hanoi_to_sapa.json");
    let resp: vexerers::TripSearchResponse = serde_json::from_str(raw).unwrap();

    let fares: Vec<FareRow> = extract_fares(&resp.data, 24, 482, "2026-09-01");
    assert!(!fares.is_empty(), "at least one fare row");

    // The number of fare rows should equal the total number of schedules
    // across all trips in the fixture.
    let expected: usize = resp.data.iter().map(|t| t.route.schedules.len()).sum();
    assert_eq!(fares.len(), expected, "one row per (trip, schedule)");

    // Every fare row should reference a trip_id that exists in the input.
    let trip_ids: std::collections::BTreeSet<String> =
        resp.data.iter().map(|t| t.id_index.clone()).collect();
    for f in &fares {
        assert!(
            trip_ids.contains(&f.trip_id),
            "fare row's trip_id {} exists in input trips",
            f.trip_id
        );
        // Price invariant: final == original - max(0, discount).
        assert_eq!(
            f.final_price_vnd,
            f.original_price_vnd - f.discount_vnd.max(0),
            "final price = original - max(0, discount)"
        );
        // Date stamped from the search filter.
        assert_eq!(f.date, "2026-09-01");
        assert_eq!(f.from_area_id, 24);
        assert_eq!(f.to_area_id, 482);
    }
}

#[test]
fn extract_fares_handles_trip_with_no_schedules() {
    // A trip with empty schedules produces zero fare rows. Built via serde_json
    // so we don't need Trip to derive Default.
    let trip: Trip = serde_json::from_value(serde_json::json!({
        "idIndex": "empty_2026-09-01_00:00_0_0",
        "route": {"id": 1, "schedules": []},
        "company": {"id": 0, "name": ""}
    }))
    .unwrap();
    let fares = extract_fares(&[trip], 0, 1, "2026-09-01");
    assert!(fares.is_empty(), "no schedules → no fare rows");
}

#[test]
fn summarize_route_computes_price_stats() {
    // Build three trips with known prices.
    let trips: Vec<Trip> = vec![
        serde_json::from_value(serde_json::json!({
            "idIndex": "t1_2026-09-01_08:00_0_1",
            "route": {
                "id": 1,
                "schedules": [{
                    "trip_code": "sch-1",
                    "hour": "08", "minute": "00",
                    "pickup_date": "2026-09-01T08:00:00+07:00",
                    "arrival_time": "2026-09-01T14:00:00+07:00",
                    "fare": {"original": 300_000, "discount": 0, "max": 300_000},
                    "available_seats": 10, "total_seats": 24,
                    "vehicle_type": "Giường nằm"
                }]
            },
            "company": {"id": 1, "name": "Op A"}
        }))
        .unwrap(),
        serde_json::from_value(serde_json::json!({
            "idIndex": "t2_2026-09-01_09:00_0_1",
            "route": {
                "id": 2,
                "schedules": [{
                    "trip_code": "sch-2",
                    "hour": "09", "minute": "00",
                    "pickup_date": "2026-09-01T09:00:00+07:00",
                    "arrival_time": "2026-09-01T15:00:00+07:00",
                    "fare": {"original": 400_000, "discount": 50_000, "max": 400_000},
                    "available_seats": 5, "total_seats": 30,
                    "vehicle_type": "Limousine"
                }]
            },
            "company": {"id": 2, "name": "Op B"}
        }))
        .unwrap(),
        serde_json::from_value(serde_json::json!({
            "idIndex": "t3_2026-09-01_10:00_0_1",
            "route": {
                "id": 3,
                "schedules": [{
                    "trip_code": "sch-3",
                    "hour": "10", "minute": "00",
                    "pickup_date": "2026-09-01T10:00:00+07:00",
                    "arrival_time": "2026-09-01T16:00:00+07:00",
                    "fare": {"original": 500_000, "discount": 0, "max": 500_000},
                    "available_seats": 15, "total_seats": 40,
                    "vehicle_type": "Xe giường"
                }]
            },
            "company": {"id": 1, "name": "Op A"}
        }))
        .unwrap(),
    ];

    let summary = summarize_route(&trips, 0, 1, "2026-09-01");
    assert_eq!(summary.n_trips, 3);
    assert_eq!(summary.n_schedules, 3);
    assert_eq!(
        summary.n_operators, 2,
        "Op A appears twice but counted once"
    );
    // Finals: 300k, 350k (400k-50k disc), 500k → min=300k, max=500k, median=350k.
    assert_eq!(summary.min_price_vnd, Some(300_000));
    assert_eq!(summary.max_price_vnd, Some(500_000), "max of final prices");
    assert_eq!(summary.median_price_vnd, Some(350_000));
    assert_eq!(summary.total_available_seats, 30, "10 + 5 + 15");
    // Avg = (300_000 + 350_000 + 500_000) / 3 = 383_333.33...
    let avg = summary.avg_price_vnd.unwrap();
    assert!((avg - 383_333.33).abs() < 1.0, "avg ~383333, got {avg}");
    assert_eq!(summary.date, "2026-09-01");
}

#[test]
fn summarize_route_empty_input_yields_all_none() {
    let summary = summarize_route(&[], 24, 482, "2026-09-01");
    assert_eq!(summary.n_trips, 0);
    assert_eq!(summary.n_schedules, 0);
    assert_eq!(summary.min_price_vnd, None);
    assert_eq!(summary.max_price_vnd, None);
    assert_eq!(summary.median_price_vnd, None);
    assert_eq!(summary.avg_price_vnd, None);
    assert_eq!(summary.total_available_seats, 0);
}

// ---------------------------------------------------------------------------
// CSV + JSONL sink round-trip via DataSinkDir.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn data_sink_dir_writes_fares_as_both_csv_and_jsonl() {
    let dir = std::env::temp_dir().join(format!(
        "vexerers-fares-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
    ));
    let _ = std::fs::remove_dir_all(&dir);
    let sink = vexerers::DataSinkDir::new(&dir);

    let fares: Vec<FareRow> = vec![
        FareRow {
            schedule_id: "sch-1".into(),
            trip_id: "t1".into(),
            route_id: 1,
            operator_id: 1,
            operator_name: "Op A".into(),
            from_area_id: 24,
            to_area_id: 482,
            date: "2026-09-01".into(),
            departure_time: "08:00".into(),
            departure_iso: "2026-09-01T08:00:00+07:00".into(),
            arrival_iso: "2026-09-01T14:00:00+07:00".into(),
            duration_minutes: Some(360),
            vehicle_type: "Giường nằm".into(),
            seat_type: 2,
            total_seats: 24,
            available_seats: 10,
            original_price_vnd: 300_000,
            discount_vnd: 0,
            final_price_vnd: 300_000,
            max_price_vnd: 300_000,
        },
        FareRow {
            schedule_id: "sch-2".into(),
            trip_id: "t2".into(),
            route_id: 2,
            operator_id: 2,
            operator_name: "Op B".into(),
            from_area_id: 24,
            to_area_id: 482,
            date: "2026-09-01".into(),
            departure_time: "09:00".into(),
            departure_iso: "2026-09-01T09:00:00+07:00".into(),
            arrival_iso: "2026-09-01T15:00:00+07:00".into(),
            duration_minutes: Some(360),
            vehicle_type: "Limousine".into(),
            seat_type: 1,
            total_seats: 30,
            available_seats: 5,
            original_price_vnd: 400_000,
            discount_vnd: 50_000,
            final_price_vnd: 350_000,
            max_price_vnd: 400_000,
        },
    ];

    let written = sink
        .write_many("fares", vexerers::SinkFormat::Both, &fares)
        .await
        .unwrap();
    assert_eq!(written, 2);

    // Verify JSONL.
    let jsonl_path = dir.join("fares.jsonl");
    assert!(jsonl_path.exists(), "fares.jsonl exists");
    let jsonl = std::fs::read_to_string(&jsonl_path).unwrap();
    assert_eq!(jsonl.lines().filter(|l| !l.is_empty()).count(), 2);
    // Round-trip the first line back into FareRow.
    let first_line = jsonl.lines().next().unwrap();
    let parsed: FareRow = serde_json::from_str(first_line).unwrap();
    assert_eq!(parsed.schedule_id, "sch-1");
    assert_eq!(parsed.final_price_vnd, 300_000);

    // Verify CSV.
    let csv_path = dir.join("fares.csv");
    assert!(csv_path.exists(), "fares.csv exists");
    let csv = std::fs::read_to_string(&csv_path).unwrap();
    let lines: Vec<&str> = csv.lines().filter(|l| !l.is_empty()).collect();
    assert_eq!(lines.len(), 3, "header + 2 data rows");
    assert!(lines[0].contains("schedule_id") && lines[0].contains("final_price_vnd"));
    assert!(lines[1].contains("sch-1"));
    assert!(lines[2].contains("sch-2"));

    std::fs::remove_dir_all(&dir).ok();
}

// Silence unused import warning when tests don't use common::.
#[allow(dead_code)]
fn _use_common_module() {
    let _ = common::page_body;
}
