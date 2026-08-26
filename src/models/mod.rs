//! Data models mirroring `VeXeRe` JSON responses.
//!
//! Designed for faithful round-trip (de)serialization: every field is
//! `Option` where the upstream is nullable, and we use `#[serde(default)]`
//! liberally so a missing field on one trip doesn't break the rest.

pub mod area;
pub mod common;
pub mod trip;

pub use area::{Area, AreaDetail, AreaDetailResponse, AreaSearchItem, AreaSearchResponse, GeoPoint, PickupPoint};
pub use common::{Company, Fare, Pagination};
pub use trip::{Route, Schedule, Trip, TripSearchResponse, TripStatus};
