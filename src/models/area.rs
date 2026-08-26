//! Area models — autocomplete (`/v3/area`) and detail (`/v1/goyolo/area/{id}`).

use serde::{Deserialize, Serialize};

/// A flexible string that accepts either a JSON string, number, or null.
///
/// VeXeRe's API is inconsistent with `*_id` fields — they're sometimes
/// returned as strings ("1926") and sometimes as ints (501095). This wrapper
/// normalizes both into a `String` (or `None` for null).
#[derive(Debug, Clone, Default, Serialize)]
pub struct FlexString(Option<String>);

impl FlexString {
    /// Access the inner string (if any).
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        self.0.as_deref()
    }
}

impl<'de> Deserialize<'de> for FlexString {
    fn deserialize<D>(d: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let v = serde_json::Value::deserialize(d)?;
        match v {
            serde_json::Value::Null => Ok(Self(None)),
            serde_json::Value::String(s) => Ok(Self(Some(s))),
            serde_json::Value::Number(n) => Ok(Self(Some(n.to_string()))),
            serde_json::Value::Bool(b) => Ok(Self(Some(b.to_string()))),
            other => Ok(Self(Some(other.to_string()))),
        }
    }
}

impl From<FlexString> for Option<String> {
    fn from(f: FlexString) -> Option<String> {
        f.0
    }
}

/// Geo coordinate pair.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GeoPoint {
    /// Latitude (WGS-84).
    #[serde(default)]
    pub lat: f64,
    /// Longitude (WGS-84).
    #[serde(default)]
    #[allow(clippy::struct_field_names)]
    pub lon: f64,
}

/// A pickup / dropoff point on a route.
///
/// These are the boarding/alighting points shown to the user when they pick a
/// trip — e.g. "Sân bay Nội Bài, Cột 14, Cảng Quốc Tế, Hà Nội".
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PickupPoint {
    /// Numeric ID of the pickup/dropoff point.
    #[serde(default)]
    pub id: i64,
    /// Minutes offset from departure time (negative = before departure, positive = after).
    #[serde(default)]
    pub duration: i32,
    /// Whether this point is hidden from the AM/PM segment.
    #[serde(default)]
    pub hidden_ams: u8,
    /// Whether this point is hidden from the AM/PM segment (BMS variant).
    #[serde(default)]
    pub hidden_bms: u8,
    /// Top-level hidden flag.
    #[serde(default)]
    pub hidden: u8,
    /// Whether this point is not at a fixed location (driver picks up anywhere).
    #[serde(default, rename = "unfixed_point")]
    pub unfixed_point: u8,
    /// District name (e.g. "Hà Nội").
    #[serde(default)]
    pub district: String,
    /// Full display name (Vietnamese).
    #[serde(default)]
    pub name: String,
    /// Lowercase-normalized name (for search).
    #[serde(default)]
    pub name_lowercase: String,
    /// WGS-84 coordinate.
    #[serde(default)]
    pub location: Option<GeoPoint>,
    /// 1 = pickup, 2 = dropoff (de facto).
    #[serde(default)]
    #[allow(clippy::struct_field_names)]
    pub r#type: u8,
    /// ID of the parent area this point belongs to (sometimes int, sometimes string).
    #[serde(default, rename = "area_id")]
    pub area_id: FlexString,
    /// English version of the name (if present).
    #[serde(default, rename = "english_name")]
    pub english_name: Option<String>,
    /// Display name as rendered by the `VeXeRe` FE.
    #[serde(default, rename = "display_name_vex")]
    pub display_name_vex: Option<String>,
    /// Lowercase of [`Self::display_name_vex`].
    #[serde(default, rename = "display_name_vex_lowercase")]
    pub display_name_vex_lowercase: Option<String>,
}

/// Top-level "Area" object (city / province / district / station).
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Area {
    /// Numeric ID as a string (`VeXeRe` uses string IDs throughout).
    #[serde(default)]
    pub id: String,
    /// Display name (Vietnamese).
    #[serde(default)]
    pub name: String,
    /// English name (optional).
    #[serde(default, rename = "english_name")]
    pub english_name: Option<String>,
    /// Short code (e.g. "HNI" for Hà Nội).
    #[serde(default, rename = "code")]
    pub code: Option<String>,
    /// Address (optional; usually empty for non-station areas).
    #[serde(default, rename = "address")]
    pub address: Option<String>,
    /// Area type: 3 = province, 4 = district, 5 = town/city.
    #[serde(default, rename = "type")]
    #[allow(clippy::struct_field_names)]
    pub r#type: Option<u8>,
    /// ID as it appears in the SEO URL slug (often == id).
    #[serde(default, rename = "id_url")]
    pub id_url: Option<serde_json::Value>,
    /// Parent city (if present).
    #[serde(default, rename = "city")]
    pub city: Option<serde_json::Value>,
    /// Parent state/province.
    #[serde(default, rename = "state")]
    pub state: Option<serde_json::Value>,
    /// Longitude.
    #[serde(default, rename = "longitude")]
    pub longitude: Option<f64>,
    /// Latitude.
    #[serde(default, rename = "latitude")]
    pub latitude: Option<f64>,
    /// Legacy migration flag.
    #[serde(default, rename = "legacy")]
    pub legacy: u8,
    /// Sub-level (1 = province-level, 2 = district-level, etc).
    #[serde(default, rename = "sub_level")]
    pub sub_level: Option<u8>,
}

/// A search hit from `GET /v3/area?q=...`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct AreaSearchItem {
    /// Numeric ID as string (e.g. "24" for Hà Nội).
    #[serde(default)]
    pub id: String,
    /// Display name (Vietnamese).
    #[serde(default)]
    pub name: String,
    /// Short code.
    #[serde(default, rename = "code")]
    pub code: Option<String>,
    /// Type: 3 = province, 4 = district, 5 = town, 1 = bus station, 2 = train station.
    #[serde(default, rename = "type")]
    #[allow(clippy::struct_field_names)]
    pub r#type: u8,
    /// Sub-type.
    #[serde(default, rename = "sub_type_id")]
    pub sub_type_id: Option<String>,
    /// Parent area ID (sometimes string, sometimes int — use [`FlexString`]).
    #[serde(default, rename = "parent_id")]
    pub parent_id: FlexString,
    /// Country ID (sometimes string, sometimes int).
    #[serde(default, rename = "country_id")]
    pub country_id: FlexString,
    /// Country code (ISO 3166-1 alpha-2, e.g. "VN").
    #[serde(default, rename = "country_code")]
    pub country_code: Option<String>,
    /// Country name.
    #[serde(default, rename = "country_name")]
    pub country_name: Option<String>,
    /// State ID (province).
    #[serde(default, rename = "state_id")]
    pub state_id: FlexString,
    /// State name.
    #[serde(default, rename = "state_name")]
    pub state_name: Option<String>,
    /// Latitude.
    #[serde(default, rename = "latitude")]
    pub latitude: Option<f64>,
    /// Longitude.
    #[serde(default, rename = "longitude")]
    pub longitude: Option<f64>,
    /// Whether the area is searchable on the FE.
    #[serde(default, rename = "is_search_on_fe")]
    pub is_search_on_fe: u8,
    /// Alternate search keys (slug variants).
    #[serde(default, rename = "merged_areas")]
    pub merged_areas: Vec<String>,
    /// Children area slug variants.
    #[serde(default, rename = "children_areas")]
    pub children_areas: Vec<String>,
    /// X-migrate key (legacy; sometimes int, sometimes string).
    #[serde(default, rename = "x_migrate_key")]
    pub x_migrate_key: FlexString,
    /// Duplicate parent ID (legacy).
    #[serde(default, rename = "duplicated_parent_id")]
    pub duplicated_parent_id: FlexString,
    /// Sub-level.
    #[serde(default, rename = "sub_level")]
    pub sub_level: Option<u8>,
    /// Catch-all for unknown fields.
    #[serde(flatten)]
    pub extra: serde_json::Value,
}

/// Response shape from `GET /v3/area?q=...`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct AreaSearchResponse {
    /// Always "success" for 2xx responses.
    #[serde(default)]
    pub message: String,
    /// List of matching areas.
    #[serde(default)]
    pub data: Vec<AreaSearchItem>,
}

/// Response shape from `GET /v1/goyolo/area/{id}` — `{ message, data: Area }`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct AreaDetailResponse {
    /// Always "success" on 2xx.
    #[serde(default)]
    pub message: String,
    /// The area detail.
    #[serde(default)]
    pub data: Area,
}

/// Convenience alias kept for API compatibility.
pub type AreaDetail = Area;
