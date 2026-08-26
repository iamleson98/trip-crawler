//! Normalize raw VeXeRe `Trip` records into flat relational tables for easy
//! ingestion into SQL databases, spreadsheets, or BI tools.
//!
//! ## Data model
//!
//! VeXeRe's native shape nests operators and prices inside each `Trip`:
//! ```text
//! Trip
//! ├── idIndex            (composite trip ID)
//! ├── route              (from/to + pickup/dropoff points + distance)
//! │   └── schedules[]    (one per departure time)
//! │       └── fare       (original, discount, max price)
//! └── company           (the bus operator, embedded)
//! ```
//!
//! This is awkward to import because:
//! - The same operator appears in thousands of trip records (denormalized).
//! - A trip can have multiple schedules, each with its own price — but
//!   typical SQL ingestion wants one row per (trip, schedule).
//!
//! This module produces two flat tables that solve both problems:
//!
//! ### `brands` (one row per unique operator)
//! Extracted by deduplicating on `company.id` across all input trips. Use as
//! the `dim_brands` dimension table — joins to `fares.operator_id`.
//!
//! ### `fares` (one row per trip × schedule)
//! The fact table. Each row = one bookable departure, with all price + seat
//! info flattened. Use as `fact_fares` — joins to `dim_brands` via
//! `operator_id`, to `dim_areas` via `from_area_id` / `to_area_id`.
//!
//! The raw `trips.jsonl` (one line per `Trip`, with full nested shape) is
//! preserved as the source of truth; the flat tables are derived views.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::models::{Company, Trip};

// ---------------------------------------------------------------------------
// Brand (operator) — flat dim table.
// ---------------------------------------------------------------------------

/// One row in the `brands` dimension table.
///
/// Extracted from the `company` field of one or more trips; deduplicated by
/// `id`. Operator-level info (name, logo, rating, phone, etc.).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Brand {
    /// Operator ID (joins to `FareRow::operator_id`).
    pub id: i64,
    /// Display name (Vietnamese).
    pub name: String,
    /// URL slug.
    pub name_slug: Option<String>,
    /// Logo URL.
    pub logo: Option<String>,
    /// Short tagline.
    pub description: Option<String>,
    /// Average rating (0–5).
    pub rating: Option<f64>,
    /// Number of ratings.
    pub rating_count: Option<u32>,
    /// Office phone.
    pub phone: Option<String>,
    /// Hotline.
    pub hotline: Option<String>,
    /// Office address.
    pub address: Option<String>,
}

impl From<&Company> for Brand {
    fn from(c: &Company) -> Self {
        Self {
            id: c.id,
            name: c.name.clone(),
            name_slug: c.name_slug.clone(),
            logo: c.logo.clone(),
            description: c.description.clone(),
            rating: c.rating,
            rating_count: c.rating_count,
            phone: c.phone.clone(),
            hotline: c.hotline.clone(),
            address: c.address.clone(),
        }
    }
}

// ---------------------------------------------------------------------------
// FareRow — flat fact table, one row per (trip, schedule).
// ---------------------------------------------------------------------------

/// One row in the `fares` fact table — one bookable departure.
///
/// Grain: `(trip_id, schedule_id)` — a trip with N schedules produces N rows.
/// Each row carries the operator (denormalized for convenience) and all
/// price + seat info flattened from the schedule.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FareRow {
    /// Schedule token (base64 opaque; matches `Schedule.trip_code`).
    /// Joins to nothing but is unique per departure.
    pub schedule_id: String,
    /// Composite trip ID (matches `Trip.id_index`).
    pub trip_id: String,
    /// Route ID (matches `Trip.route.id`).
    pub route_id: i64,
    /// Operator ID (joins to `Brand.id`).
    pub operator_id: i64,
    /// Operator name (denormalized for convenience).
    pub operator_name: String,
    /// From-area ID (from the search filter that produced this trip).
    pub from_area_id: u64,
    /// To-area ID (from the search filter).
    pub to_area_id: u64,
    /// Departure date (YYYY-MM-DD, from the search filter).
    pub date: String,

    /// Departure time `HH:MM` (24h, zero-padded).
    pub departure_time: String,
    /// ISO 8601 departure timestamp (e.g. `2026-09-01T23:00:00+07:00`).
    pub departure_iso: String,
    /// ISO 8601 arrival timestamp.
    pub arrival_iso: String,
    /// Trip duration in minutes (computed from departure + arrival).
    pub duration_minutes: Option<i64>,
    /// Vehicle type label (e.g. "Giường nằm 24 chỗ").
    pub vehicle_type: String,
    /// Seat type code (1 = seat, 2 = bed, etc.).
    pub seat_type: u8,
    /// Total seats in the vehicle.
    pub total_seats: u32,
    /// Currently available seats.
    pub available_seats: u32,

    /// Original sticker price in VND.
    pub original_price_vnd: i64,
    /// Discount amount in VND (subtract from original to get final).
    pub discount_vnd: i64,
    /// Final price after discount (= original - max(0, discount)).
    pub final_price_vnd: i64,
    /// Max price (used by VeXeRe for sorting; usually == original).
    pub max_price_vnd: i64,
}

// ---------------------------------------------------------------------------
// Aggregations / summaries.
// ---------------------------------------------------------------------------

/// Per-route aggregation across all schedules on a date. One row per
/// (from, to, date). Useful for quick "what's the price range on this route"
/// queries without re-parsing the full fares table.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct RouteSummary {
    /// From-area ID.
    pub from_area_id: u64,
    /// To-area ID.
    pub to_area_id: u64,
    /// Date (YYYY-MM-DD).
    pub date: String,
    /// Number of trips (parent trips; one trip = one row in `trips.jsonl`).
    pub n_trips: usize,
    /// Number of bookable departures (= number of `FareRow`s).
    pub n_schedules: usize,
    /// Number of distinct operators.
    pub n_operators: usize,
    /// Lowest final price (VND) across all schedules.
    pub min_price_vnd: Option<i64>,
    /// Highest final price (VND) across all schedules.
    pub max_price_vnd: Option<i64>,
    /// Average final price (VND) across all schedules.
    pub avg_price_vnd: Option<f64>,
    /// Median final price (VND) — robust against outliers.
    pub median_price_vnd: Option<i64>,
    /// Total seats available across all schedules.
    pub total_available_seats: u64,
}

// ---------------------------------------------------------------------------
// Extraction functions.
// ---------------------------------------------------------------------------

/// Deduplicate operators across a batch of trips, keyed by `company.id`.
///
/// If the same operator appears with different fields across trips (rare but
/// possible — VeXeRe occasionally returns partial company info), the **first
/// non-empty / non-null** value for each field wins. This ensures we keep the
/// most complete info we've seen.
#[must_use]
pub fn extract_brands(trips: &[Trip]) -> Vec<Brand> {
    let mut by_id: BTreeMap<i64, Brand> = BTreeMap::new();
    for t in trips {
        // Skip the placeholder operator (id == 0) VeXeRe uses for trips that
        // don't have a company associated.
        if t.company.id == 0 {
            continue;
        }
        let next = Brand::from(&t.company);
        match by_id.get(&next.id) {
            None => {
                by_id.insert(next.id, next);
            }
            Some(prev) => {
                // Merge: take the "richer" version (more filled fields).
                let merged = merge_brand(prev, &next);
                by_id.insert(next.id, merged);
            }
        }
    }
    by_id.into_values().collect()
}

/// Flatten `trips × schedules` into one `FareRow` per departure.
///
/// `from`, `to`, and `date` are the search-filter values that produced these
/// trips — they're stamped onto every emitted row so the fares table is
/// self-contained (no need to consult the search log).
#[must_use]
pub fn extract_fares(trips: &[Trip], from: u64, to: u64, date: &str) -> Vec<FareRow> {
    let mut rows = Vec::with_capacity(trips.len().saturating_mul(2));
    for t in trips {
        let trip_id = t.id_index.clone();
        let route_id = t.route.id;
        let operator_id = t.company.id;
        let operator_name = t.company.name.clone();
        for s in &t.route.schedules {
            let departure_time = format!("{}:{}", s.hour, s.minute);
            let duration_minutes = compute_duration_minutes(&s.pickup_date, &s.arrival_time);
            let final_price_vnd = s.fare.original - s.fare.discount.max(0);
            rows.push(FareRow {
                schedule_id: s.trip_code.clone(),
                trip_id: trip_id.clone(),
                route_id,
                operator_id,
                operator_name: operator_name.clone(),
                from_area_id: from,
                to_area_id: to,
                date: date.to_string(),
                departure_time,
                departure_iso: s.pickup_date.clone(),
                arrival_iso: s.arrival_time.clone(),
                duration_minutes,
                vehicle_type: s.vehicle_type.clone(),
                seat_type: s.seat_type,
                total_seats: s.total_seats,
                available_seats: s.available_seats,
                original_price_vnd: s.fare.original,
                discount_vnd: s.fare.discount,
                final_price_vnd,
                max_price_vnd: s.fare.max,
            });
        }
    }
    rows
}

/// Aggregate a batch of trips (for one route × date) into a `RouteSummary`.
///
/// Computes count + price stats (min / max / avg / median) + total seats
/// across all schedules. Empty input yields an all-`None` summary.
#[must_use]
pub fn summarize_route(trips: &[Trip], from: u64, to: u64, date: &str) -> RouteSummary {
    let fares = extract_fares(trips, from, to, date);
    summarize_fares(&fares, from, to, date)
}

/// Same as [`summarize_route`] but operates on already-flattened fares —
/// useful when re-aggregating after a re-write of the fares table.
#[must_use]
pub fn summarize_fares(fares: &[FareRow], from: u64, to: u64, date: &str) -> RouteSummary {
    if fares.is_empty() {
        return RouteSummary {
            from_area_id: from,
            to_area_id: to,
            date: date.to_string(),
            ..RouteSummary::default()
        };
    }
    let mut prices: Vec<i64> = fares.iter().map(|f| f.final_price_vnd).collect();
    prices.sort_unstable();
    let n_schedules = fares.len();
    let n_trips = fares
        .iter()
        .map(|f| f.trip_id.as_str())
        .collect::<std::collections::BTreeSet<_>>()
        .len();
    let n_operators = fares
        .iter()
        .map(|f| f.operator_id)
        .collect::<std::collections::BTreeSet<_>>()
        .len();
    let min_price = prices.first().copied();
    let max_price = prices.last().copied();
    let sum: i128 = prices.iter().map(|p| i128::from(*p)).sum();
    let avg = sum as f64 / n_schedules as f64;
    let median = median_sorted(&prices);
    let total_seats: u64 = fares.iter().map(|f| u64::from(f.available_seats)).sum();
    RouteSummary {
        from_area_id: from,
        to_area_id: to,
        date: date.to_string(),
        n_trips,
        n_schedules,
        n_operators,
        min_price_vnd: min_price,
        max_price_vnd: max_price,
        avg_price_vnd: Some(avg),
        median_price_vnd: median,
        total_available_seats: total_seats,
    }
}

// ---------------------------------------------------------------------------
// Internal helpers.
// ---------------------------------------------------------------------------

/// Merge two `Brand`s — for each field, take the first non-empty / non-None
/// value. Used to consolidate partial operator info seen across many trips.
fn merge_brand(a: &Brand, b: &Brand) -> Brand {
    Brand {
        id: a.id,
        name: pick_nonempty(&a.name, &b.name),
        name_slug: a.name_slug.clone().or_else(|| b.name_slug.clone()),
        logo: a.logo.clone().or_else(|| b.logo.clone()),
        description: a.description.clone().or_else(|| b.description.clone()),
        rating: a.rating.or(b.rating),
        rating_count: a.rating_count.or(b.rating_count),
        phone: a.phone.clone().or_else(|| b.phone.clone()),
        hotline: a.hotline.clone().or_else(|| b.hotline.clone()),
        address: a.address.clone().or_else(|| b.address.clone()),
    }
}

fn pick_nonempty(a: &str, b: &str) -> String {
    if a.is_empty() {
        b.to_string()
    } else {
        a.to_string()
    }
}

/// Compute duration in minutes between two ISO 8601 timestamps.
///
/// Returns `None` if either input is empty / unparseable.
fn compute_duration_minutes(start_iso: &str, end_iso: &str) -> Option<i64> {
    if start_iso.is_empty() || end_iso.is_empty() {
        return None;
    }
    let start = parse_iso(start_iso)?;
    let end = parse_iso(end_iso)?;
    let delta_secs = end - start;
    Some(delta_secs / 60)
}

/// Parse an ISO 8601 timestamp like `2026-09-01T23:00:00+07:00` into a
/// `chrono::DateTime` equivalent (here, a unix-seconds `i64` since we don't
/// pull in chrono — see below).
///
/// **Note**: we avoid pulling in `chrono` for one function. The parsing here
/// is a minimal RFC-3339-ish parser; if it fails we return `None` and the
/// caller falls back to "unknown duration" gracefully.
fn parse_iso(s: &str) -> Option<i64> {
    // Lightweight ISO-8601 parser.
    // Accepts: YYYY-MM-DDTHH:MM:SS[.fff][+HH:MM | Z]
    let bytes = s.as_bytes();
    if bytes.len() < 19 {
        return None;
    }
    let year: i64 = s.get(0..4)?.parse().ok()?;
    if bytes[4] != b'-' {
        return None;
    }
    let month: i64 = s.get(5..7)?.parse().ok()?;
    if bytes[7] != b'-' {
        return None;
    }
    let day: i64 = s.get(8..10)?.parse().ok()?;
    if bytes[10] != b'T' && bytes[10] != b' ' {
        return None;
    }
    let hour: i64 = s.get(11..13)?.parse().ok()?;
    if bytes[13] != b':' {
        return None;
    }
    let min: i64 = s.get(14..16)?.parse().ok()?;
    if bytes[16] != b':' {
        return None;
    }
    let sec: i64 = s.get(17..19)?.parse().ok()?;
    // Skip fractional seconds if any.
    let mut idx = 19;
    if idx < bytes.len() && bytes[idx] == b'.' {
        idx += 1;
        while idx < bytes.len() && bytes[idx].is_ascii_digit() {
            idx += 1;
        }
    }
    // Parse optional timezone.
    let mut tz_offset_sec: i64 = 0;
    if idx < bytes.len() {
        match bytes[idx] {
            b'+' | b'-' => {
                let sign: i64 = if bytes[idx] == b'-' { -1 } else { 1 };
                idx += 1;
                if idx + 5 > bytes.len() {
                    return None;
                }
                let tz_h: i64 = s.get(idx..idx + 2)?.parse().ok()?;
                if bytes[idx + 2] != b':' {
                    return None;
                }
                let tz_m: i64 = s.get(idx + 3..idx + 5)?.parse().ok()?;
                tz_offset_sec = sign * (tz_h * 3600 + tz_m * 60);
            }
            // Z / z / anything else → no offset (treat as UTC).
            _ => {}
        }
    }
    let utc_seconds =
        days_from_civil(year, month, day) * 86_400 + hour * 3600 + min * 60 + sec - tz_offset_sec;
    Some(utc_seconds)
}

/// Convert (year, month, day) [Gregorian] to days since 1970-01-01.
///
/// Implements Howard Hinnant's `days_from_civil` algorithm — same as what
/// `chrono` uses internally. Returns a signed day count (negative = pre-1970).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy as u64;
    era * 146_097 + doe as i64 - 719_468
}

/// Median of a sorted slice.
fn median_sorted(sorted: &[i64]) -> Option<i64> {
    let n = sorted.len();
    if n == 0 {
        return None;
    }
    if n % 2 == 1 {
        Some(sorted[n / 2])
    } else {
        // Average the two middle elements; round down (we're i64).
        let a = sorted[n / 2 - 1];
        let b = sorted[n / 2];
        Some(a.saturating_add(b) / 2)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_brand_prefers_non_empty_strings() {
        let a = Brand {
            id: 1,
            name: String::new(),
            name_slug: None,
            logo: Some("logo-a.png".into()),
            description: Some("from A".into()),
            rating: None,
            rating_count: None,
            phone: None,
            hotline: None,
            address: None,
        };
        let b = Brand {
            id: 1,
            name: "Operator B".into(),
            name_slug: Some("op-b".into()),
            logo: None,
            description: None,
            rating: Some(4.5),
            rating_count: Some(10),
            phone: Some("0901".into()),
            hotline: Some("1900".into()),
            address: Some("123 X".into()),
        };
        let m = merge_brand(&a, &b);
        assert_eq!(m.name, "Operator B"); // a's name was empty, b's wins
        assert_eq!(m.logo.as_deref(), Some("logo-a.png")); // a had logo, kept
        assert_eq!(m.description.as_deref(), Some("from A"));
        assert_eq!(m.rating, Some(4.5));
        assert_eq!(m.address.as_deref(), Some("123 X"));
    }

    #[test]
    fn parse_iso_handles_full_offset() {
        let t = parse_iso("2026-09-01T23:00:00+07:00").unwrap();
        let u = parse_iso("2026-09-02T05:00:00+07:00").unwrap();
        assert_eq!(u - t, 6 * 3600);
    }

    #[test]
    fn parse_iso_handles_zulu() {
        let t = parse_iso("2026-09-01T23:00:00Z").unwrap();
        assert!(t > 1_700_000_000); // some sane epoch timestamp
    }

    #[test]
    fn parse_iso_returns_none_for_empty() {
        assert!(parse_iso("").is_none());
        assert!(parse_iso("garbage").is_none());
    }

    #[test]
    fn compute_duration_is_minutes_between() {
        let d = compute_duration_minutes("2026-09-01T23:00:00+07:00", "2026-09-02T05:00:00+07:00");
        assert_eq!(d, Some(360));
    }

    #[test]
    fn median_handles_odd_and_even() {
        assert_eq!(median_sorted(&[100, 200, 300]), Some(200));
        assert_eq!(median_sorted(&[100, 200, 300, 400]), Some(250));
        assert_eq!(median_sorted(&[]), None);
    }
}
