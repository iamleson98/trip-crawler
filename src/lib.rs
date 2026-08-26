//! # vexerers
//!
//! A reverse-engineered Rust client + crawler for the `VeXeRe` (`vexere.com`) public bus-trip
//! APIs. Anonymous-only — no booking, no payment, no authed endpoints.
//!
//! ## What's covered
//!
//! | Surface | Endpoint | Module |
//! |---------|----------|--------|
//! | Anonymous bearer token | `POST https://vexere.com/getToken` | [`api::auth`] |
//! | Area autocomplete | `GET internal-vroute.vexere.com/v3/area` | [`api::area`] |
//! | Area detail | `GET internal-vroute.vexere.com/v1/goyolo/area/{id}` | [`api::area`] |
//! | **Bus trip search** | `GET internal-vroute.vexere.com/v2/route/` | [`api::trips`] |
//! | Bus / flight / train counts | `GET internal-vroute.vexere.com/v2/route/{kind}/count` | [`api::counts`] |
//! | Home popular routes / featured bus | `POST vexere.com/vi-VN/Home/{PopularRoute,FeaturedBusInfo,...}` | [`api::home`] |
//!
//! ## Quick start
//!
//! ```no_run
//! # #[tokio::main]
//! # async fn main() -> anyhow::Result<()> {
//! use vexerers::{Config, VexereClient};
//!
//! let cfg = Config::default();
//! let client = VexereClient::new(cfg)?;
//!
//! let trips = client.trips().search(24, 482, "2026-08-27").await?;
//! println!("found {} trips", trips.total);
//! for t in trips.data.iter().take(5) {
//!     println!("- {:?}", t.id_index);
//! }
//! # Ok(())
//! # }
//! ```

#![warn(missing_docs)]
#![warn(clippy::all, clippy::pedantic)]
#![allow(
    clippy::module_name_repetitions,
    clippy::missing_errors_doc,
    clippy::missing_panics_doc,
    clippy::must_use_candidate,
    clippy::doc_markdown,
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss,
    clippy::cast_precision_loss
)]

pub mod api;
pub mod client;
pub mod config;
pub mod error;
pub mod extract;
pub mod models;
pub mod sink;

pub use client::VexereClient;
pub use config::{ConcurrencyProfile, Config, Endpoints};
pub use error::{Error, Result};
pub use extract::{extract_brands, extract_fares, summarize_route, Brand, FareRow, RouteSummary};
pub use models::{
    Area, AreaDetail, AreaDetailResponse, AreaSearchItem, AreaSearchResponse, Fare, PickupPoint,
    Route, Schedule, Trip, TripSearchResponse,
};
pub use sink::{CsvSink, DataSinkDir, JsonlSink, JsonlSinkDir, SinkFormat};

/// Crate version (matches `env!("CARGO_PKG_VERSION")`).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
