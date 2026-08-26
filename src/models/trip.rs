//! Trip models — the core payload of the crawler.

use serde::{Deserialize, Serialize};

use super::{Fare, PickupPoint};
use crate::models::common::Company;

/// One departure schedule for a trip.
///
/// A `Trip` may have multiple schedules (e.g. a 22:50 departure and a 23:30
/// departure on the same route+date). Each schedule has its own price,
/// available seats, and seat template.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Schedule {
    /// Base64-encoded opaque token that uniquely identifies this schedule.
    /// Used for booking flows (not anonymous).
    #[serde(default, rename = "trip_code")]
    pub trip_code: String,
    /// Departure hour (24h, zero-padded string).
    #[serde(default)]
    pub hour: String,
    /// Departure minute (zero-padded string).
    #[serde(default)]
    pub minute: String,
    /// ISO 8601 departure timestamp with +07:00 offset.
    #[serde(default, rename = "pickup_date")]
    pub pickup_date: String,
    /// ISO 8601 arrival timestamp.
    #[serde(default, rename = "arrival_time")]
    pub arrival_time: String,
    /// Price info.
    #[serde(default)]
    pub fare: Fare,
    /// Deposit selling price (0 if not applicable).
    #[serde(default, rename = "deposit_selling")]
    pub deposit_selling: i64,
    /// Booking config ("ONLINE" / "OFFLINE").
    #[serde(default, rename = "config")]
    pub config: String,
    /// Seat type (1 = seat, 2 = bed, etc).
    #[serde(default, rename = "seat_type")]
    pub seat_type: u8,
    /// Currently available seats.
    #[serde(default, rename = "available_seats")]
    pub available_seats: u32,
    /// Total available seats across all schedules (used for sorting).
    #[serde(default, rename = "total_available_seats")]
    pub total_available_seats: u32,
    /// Vehicle quality (1 = standard, 2 = limousine, etc).
    #[serde(default, rename = "vehicle_quality")]
    pub vehicle_quality: u8,
    /// Total seats in the vehicle.
    #[serde(default, rename = "total_seats")]
    pub total_seats: u32,
    /// 1 if the fare is being updated; 0 otherwise.
    #[serde(default, rename = "is_updating_fare")]
    pub is_updating_fare: u8,
    /// Vehicle type description (e.g. "Limousine 24 chỗ").
    #[serde(default, rename = "vehicle_type")]
    pub vehicle_type: String,
    /// Seat-template name.
    #[serde(default, rename = "seat_template_name")]
    pub seat_template_name: String,
    /// Seat-template ID.
    #[serde(default, rename = "seat_template_id")]
    pub seat_template_id: i64,
    /// 1 if the fare is refundable.
    #[serde(default, rename = "refundable")]
    pub refundable: u8,
    /// 1 if seat selection is not allowed.
    #[serde(default, rename = "unchoosable")]
    pub unchoosable: u8,
    /// Whether this schedule is "enhanced".
    #[serde(default, rename = "is_enhance")]
    pub is_enhance: bool,
    /// Whether last-minute activation is on.
    #[serde(default, rename = "is_last_minute_activated")]
    pub is_last_minute_activated: u8,
    /// Whether early-bird promo is active.
    #[serde(default, rename = "is_early_bird_activated")]
    pub is_early_bird_activated: u8,
    /// Min fill rate for early-bird promo.
    #[serde(default, rename = "early_bird_min_fill_rate")]
    pub early_bird_min_fill_rate: u32,
    /// Catch-all.
    #[serde(flatten)]
    pub extra: serde_json::Value,
}

/// A route = the trip path (from → to with pickup/dropoff points + schedule list).
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Route {
    /// Route ID.
    #[serde(default)]
    pub id: i64,
    /// Display name (e.g. " Bắc Giang-Sapa ").
    #[serde(default)]
    pub name: String,
    /// Pickup (boarding) points.
    #[serde(default, rename = "pickup_points")]
    pub pickup_points: Vec<PickupPoint>,
    /// Dropoff (alighting) points.
    #[serde(default, rename = "dropoff_points")]
    pub dropoff_points: Vec<PickupPoint>,
    /// Payment methods accepted on this route.
    #[serde(default, rename = "payment_method")]
    pub payment_method: Option<serde_json::Value>,
    /// Distance in km.
    #[serde(default, rename = "distance")]
    pub distance: Option<f64>,
    /// ISO 8601 date portion of departure.
    #[serde(default, rename = "departure_date")]
    pub departure_date: Option<String>,
    /// Departure time (HH:MM).
    #[serde(default, rename = "departure_time")]
    pub departure_time: Option<String>,
    /// Vehicle quality (mirror of `Schedule::vehicle_quality`).
    #[serde(default, rename = "vehicle_quality")]
    pub vehicle_quality: Option<u8>,
    /// From-area object (compact).
    #[serde(default, rename = "from")]
    pub from: Option<serde_json::Value>,
    /// To-area object (compact).
    #[serde(default, rename = "to")]
    pub to: Option<serde_json::Value>,
    /// Trip duration (minutes).
    #[serde(default, rename = "duration")]
    pub duration: Option<i64>,
    /// 1 if fare is still being computed.
    #[serde(default, rename = "is_updating_fare")]
    pub is_updating_fare: u8,
    /// Sort rank (lower = more prominent).
    #[serde(default, rename = "sorted")]
    pub sorted: Option<i32>,
    /// Full trip metadata (legacy).
    #[serde(default, rename = "full_trip")]
    pub full_trip: Option<serde_json::Value>,
    /// 11.11 sale flag (returned as a JSON boolean by the API).
    #[serde(default, rename = "is_deal_1111")]
    pub is_deal_1111: bool,
    /// English-speaking utility flag (returned as a JSON boolean by the API).
    #[serde(default, rename = "speaking_english_utility")]
    pub speaking_english_utility: bool,
    /// Departure schedules for this route (usually 1 entry).
    #[serde(default, rename = "schedules")]
    pub schedules: Vec<Schedule>,
    /// Catch-all.
    #[serde(flatten)]
    pub extra: serde_json::Value,
}

/// Status of a trip (`VeXeRe` encodes as int).
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
pub struct TripStatus(pub u8);

impl std::fmt::Display for TripStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A single trip result returned by `GET /v2/route/`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Trip {
    /// Composite ID like "351932250_2026-08-27_23:00_24_38".
    #[serde(default, rename = "idIndex")]
    pub id_index: String,
    /// Route info (path + schedules).
    #[serde(default)]
    pub route: Route,
    /// Operator info.
    #[serde(default)]
    pub company: Company,
    /// Max total seats across schedules.
    #[serde(default, rename = "max_total_seats")]
    pub max_total_seats: u32,
    /// Trip status (encoded).
    #[serde(default)]
    pub status: u8,
    /// 1 if sponsored.
    #[serde(default, rename = "is_sponsor")]
    pub is_sponsor: u8,
    /// Coupons available.
    #[serde(default, rename = "has_coupons")]
    pub has_coupons: u8,
    /// Coupons for AMS (anti-money-laundering segment).
    #[serde(default, rename = "has_coupons_ams")]
    pub has_coupons_ams: u8,
    /// Coupon list (raw).
    #[serde(default, rename = "coupons")]
    pub coupons: serde_json::Value,
    /// Has "cop" (commission on partnership).
    #[serde(default, rename = "has_cop")]
    pub has_cop: u8,
    /// Search-ranking score.
    #[serde(default, rename = "score")]
    pub score: f64,
    /// CTR score (raw).
    #[serde(default, rename = "ctr_score")]
    pub ctr_score: serde_json::Value,
    /// CVR score (raw).
    #[serde(default, rename = "cvr_score")]
    pub cvr_score: serde_json::Value,
    /// Discount score (float in [0,1]).
    #[serde(default, rename = "discount_score")]
    pub discount_score: f64,
    /// Coupon score (float).
    #[serde(default, rename = "coupon_score")]
    pub coupon_score: f64,
    /// Search-ranking commission score.
    #[serde(default, rename = "commission_score")]
    pub commission_score: Option<f64>,
    /// Commission score normalization (sometimes int, sometimes float).
    #[serde(default, rename = "commission_score_normalization")]
    pub commission_score_normalization: Option<f64>,
    /// Rating score normalization (float).
    #[serde(default, rename = "rating_score_normalization")]
    pub rating_score_normalization: Option<f64>,
    /// Trip rating score normalization (float).
    #[serde(default, rename = "trip_rating_score_normalization")]
    pub trip_rating_score_normalization: Option<f64>,
    /// Penalty score normalization (sometimes int, sometimes float).
    #[serde(default, rename = "penalty_score_normalization")]
    pub penalty_score_normalization: Option<f64>,
    /// Social-proof: recent booking number.
    #[serde(default, rename = "social_proof_booking_number")]
    pub social_proof_booking_number: serde_json::Value,
    /// Social-proof: rating list.
    #[serde(default, rename = "social_proof_ratings")]
    pub social_proof_ratings: serde_json::Value,
    /// Catch-all for forward-compat.
    #[serde(flatten)]
    pub extra: serde_json::Value,
}

impl Trip {
    /// Convenience: the best (lowest) fare across this trip's schedules, in VND.
    #[must_use]
    pub fn best_fare_vnd(&self) -> Option<i64> {
        self.route
            .schedules
            .iter()
            .filter_map(|s| {
                if s.fare.original > 0 {
                    Some(s.fare.original - s.fare.discount.max(0))
                } else {
                    None
                }
            })
            .min()
    }

    /// Convenience: total available seats across this trip's schedules.
    #[must_use]
    pub fn total_available_seats(&self) -> u32 {
        self.route.schedules.iter().map(|s| s.available_seats).sum()
    }

    /// Convenience: vehicle type label (e.g. "Limousine 24 chỗ").
    #[must_use]
    pub fn vehicle_type(&self) -> Option<&str> {
        self.route
            .schedules
            .first()
            .map(|s| s.vehicle_type.as_str())
    }
}

/// Top-level response of `GET /v2/route/` (bus trip search).
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TripSearchResponse {
    /// Always "success" on 2xx.
    #[serde(default)]
    pub message: String,
    /// Page of trips.
    #[serde(default)]
    pub data: Vec<Trip>,
    /// Current page (1-indexed).
    #[serde(default)]
    pub page: u32,
    /// Page size used by the request.
    #[serde(default, rename = "page_size")]
    pub page_size: u32,
    /// Total trips across all pages.
    #[serde(default, rename = "total")]
    pub total: u32,
    /// Total number of pages.
    #[serde(default, rename = "total_pages")]
    pub total_pages: u32,
    /// From + To area objects.
    #[serde(default)]
    pub area: serde_json::Value,
    /// Operators appearing in this result set.
    #[serde(default)]
    pub companies: serde_json::Value,
    /// Diversity metadata.
    #[serde(default)]
    pub diversity: serde_json::Value,
    /// Diversity version.
    #[serde(default, rename = "diversity_version")]
    pub diversity_version: Option<serde_json::Value>,
}
