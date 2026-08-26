//! Common small types shared across responses.

use serde::Deserialize;
use serde::Serialize;

/// Generic pagination wrapper (used by trip search).
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Pagination {
    /// Current page (1-indexed).
    pub page: u32,
    /// Items per page.
    #[serde(rename = "page_size")]
    pub page_size: u32,
    /// Total items across all pages.
    #[serde(rename = "total")]
    pub total: u32,
    /// Total number of pages.
    #[serde(rename = "total_pages")]
    pub total_pages: u32,
}

/// Fare (price) info.
///
/// `VeXeRe` returns three prices per schedule:
/// - `original`: full sticker price
/// - `discount`: amount discounted (can be -1 meaning "no discount available")
/// - `max`: max(price) for sorting purposes
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Fare {
    /// Original sticker price in VND.
    #[serde(default)]
    pub original: i64,
    /// Discount amount in VND (negative = discount; -1 means no discount).
    #[serde(default)]
    pub discount: i64,
    /// Maximum price (used for sorting; usually == original).
    #[serde(default)]
    pub max: i64,
}

/// Bus operator (carrier / "hãng xe").
///
/// Most fields are nullable across the `VeXeRe` dataset; we keep them as `Option`.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Company {
    /// Numeric company ID.
    #[serde(default)]
    pub id: i64,
    /// Operator display name (e.g. "Minh Trí Limousine").
    #[serde(default)]
    pub name: String,
    /// Slug used in URLs.
    #[serde(default, rename = "name_slug")]
    pub name_slug: Option<String>,
    /// Logo URL.
    #[serde(default, rename = "logo")]
    pub logo: Option<String>,
    /// Short description / tagline.
    #[serde(default, rename = "description")]
    pub description: Option<String>,
    /// Average rating (0-5).
    #[serde(default, rename = "rating")]
    pub rating: Option<f64>,
    /// Number of ratings.
    #[serde(default, rename = "rating_count")]
    pub rating_count: Option<u32>,
    /// Office phone number.
    #[serde(default, rename = "phone")]
    pub phone: Option<String>,
    /// Hotline.
    #[serde(default, rename = "hotline")]
    pub hotline: Option<String>,
    /// Address of head office.
    #[serde(default, rename = "address")]
    pub address: Option<String>,
    /// Pickup/dropoff office information.
    #[serde(default, rename = "offices")]
    pub offices: Option<serde_json::Value>,
    /// Free-form properties from upstream.
    #[serde(flatten)]
    pub extra: serde_json::Value,
}
