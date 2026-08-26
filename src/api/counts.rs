//! `GET /v2/route/{kind}/count` — bus/flight/train counts grouped by date.

use serde::{Deserialize, Serialize};
use url::Url;

use crate::client::VexereClient;
use crate::error::Result;

/// Vehicle kind for the count endpoint.
#[derive(Debug, Clone, Copy)]
pub enum CountKind {
    /// Bus / coach.
    Bus,
    /// Flight.
    Flight,
    /// Train.
    Train,
}

impl CountKind {
    fn path(self) -> &'static str {
        match self {
            Self::Bus => "bus",
            Self::Flight => "flight",
            Self::Train => "train",
        }
    }
}

/// Request shape for `GET /v2/route/{kind}/count`.
#[derive(Debug, Clone)]
pub struct CountRequest {
    /// From-area ID.
    pub from: u64,
    /// To-area ID.
    pub to: u64,
    /// Number of passengers (default 1).
    pub quantity: u32,
    /// Date in `YYYY-MM-DD`.
    pub date: String,
    /// Page (1-indexed).
    pub page: u32,
    /// Page size (default 50).
    pub page_size: u32,
}

impl CountRequest {
    /// Construct a count request for one passenger on the given date.
    #[must_use]
    pub fn new(from: u64, to: u64, date: impl Into<String>) -> Self {
        Self {
            from,
            to,
            quantity: 1,
            date: date.into(),
            page: 1,
            page_size: 50,
        }
    }
}

/// Response shape: `{ message: "success", data: [{ grouped_by_date: {...} }] }`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CountResponse {
    /// "success" on 2xx.
    #[serde(default)]
    pub message: String,
    /// List with a single entry containing `grouped_by_date` map.
    #[serde(default)]
    pub data: Vec<serde_json::Value>,
}

/// Handle for the bus/flight/train count endpoint.
#[derive(Clone)]
pub struct Counts {
    client: VexereClient,
}

impl Counts {
    pub(crate) fn new(client: VexereClient) -> Self {
        Self { client }
    }

    /// Get trip counts for a vehicle kind grouped by date.
    pub async fn count(&self, kind: CountKind, req: &CountRequest) -> Result<CountResponse> {
        let mut url: Url = self
            .client
            .endpoints()
            .trip_service
            .join(&format!("v2/route/{}/count", kind.path()))?;
        let mut q = url.query_pairs_mut();
        q.append_pair("filter[from]", &req.from.to_string());
        q.append_pair("filter[to]", &req.to.to_string());
        q.append_pair("filter[quantity]", &req.quantity.to_string());
        q.append_pair("filter[date]", &req.date);
        q.append_pair("is_trigger_crawl", "1");
        q.append_pair("page", &req.page.to_string());
        q.append_pair("pagesize", &req.page_size.to_string());
        drop(q);
        self.client.get_internal(&url).await
    }

    /// Convenience: bus trip counts.
    pub async fn bus(&self, from: u64, to: u64, date: &str) -> Result<CountResponse> {
        self.count(CountKind::Bus, &CountRequest::new(from, to, date)).await
    }

    /// Convenience: flight counts.
    pub async fn flight(&self, from: u64, to: u64, date: &str) -> Result<CountResponse> {
        self.count(CountKind::Flight, &CountRequest::new(from, to, date)).await
    }

    /// Convenience: train counts.
    pub async fn train(&self, from: u64, to: u64, date: &str) -> Result<CountResponse> {
        self.count(CountKind::Train, &CountRequest::new(from, to, date)).await
    }
}
