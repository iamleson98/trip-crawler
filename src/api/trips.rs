//! `GET /v2/route/` — the canonical bus-trip search endpoint.
//!
//! Full example:
//! ```http
//! GET https://internal-vroute.vexere.com/v2/route/
//!   ?filter[from]=24
//!   &filter[to]=482
//!   &filter[quantity]=1
//!   &filter[date]=2026-08-27
//!   &page=1
//!   &pagesize=50
//! ```

use serde::{Deserialize, Serialize};
use url::Url;

use crate::client::VexereClient;
use crate::error::{Error, Result};
use crate::models::TripSearchResponse;

/// Filter params for a trip search.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TripFilter {
    /// From-area ID (e.g. 24 = Hà Nội).
    pub from: u64,
    /// To-area ID (e.g. 482 = Sa Pa).
    pub to: u64,
    /// Number of passengers (default 1).
    pub quantity: u32,
    /// ISO date `YYYY-MM-DD`.
    pub date: String,
}

impl TripFilter {
    /// Construct a filter for a single passenger on the given date.
    #[must_use]
    pub fn new(from: u64, to: u64, date: impl Into<String>) -> Self {
        Self {
            from,
            to,
            quantity: 1,
            date: date.into(),
        }
    }

    /// Override the passenger count.
    #[must_use]
    pub fn with_quantity(mut self, q: u32) -> Self {
        self.quantity = q;
        self
    }

    /// Validate the date format (`YYYY-MM-DD`).
    fn validate(&self) -> Result<()> {
        let bytes = self.date.as_bytes();
        if bytes.len() != 10
            || bytes[4] != b'-'
            || bytes[7] != b'-'
            || !bytes[..4].iter().all(u8::is_ascii_digit)
            || !bytes[5..7].iter().all(u8::is_ascii_digit)
            || !bytes[8..10].iter().all(u8::is_ascii_digit)
        {
            return Err(Error::InvalidArgument(format!(
                "date must be YYYY-MM-DD, got {:?}",
                self.date
            )));
        }
        Ok(())
    }
}

/// Optional pagination + sort for a trip search.
#[derive(Debug, Clone, Copy)]
pub struct TripSearchOptions {
    /// 1-indexed page (default 1).
    pub page: u32,
    /// Page size (max 50; default 50).
    pub page_size: u32,
}

impl Default for TripSearchOptions {
    fn default() -> Self {
        Self {
            page: 1,
            page_size: 50,
        }
    }
}

impl TripSearchOptions {
    /// Construct a `page=1, page_size=50` options block.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Set page.
    #[must_use]
    pub fn with_page(mut self, page: u32) -> Self {
        self.page = page.max(1);
        self
    }

    /// Set page size (clamped to 1..=50).
    #[must_use]
    pub fn with_page_size(mut self, n: u32) -> Self {
        self.page_size = n.clamp(1, 50);
        self
    }
}

/// Handle for bus-trip search endpoints.
#[derive(Clone)]
pub struct Trips {
    client: VexereClient,
}

impl Trips {
    pub(crate) fn new(client: VexereClient) -> Self {
        Self { client }
    }

    /// Search bus trips for a single day.
    ///
    /// ```no_run
    /// # #[tokio::main] async fn main() -> anyhow::Result<()> {
    /// # use vexerers::{Config, VexereClient};
    /// # let c = VexereClient::new(Config::default())?;
    /// let r = c.trips().search(24, 482, "2026-08-27").await?;
    /// println!("{} trips", r.total);
    /// # Ok(())
    /// # }
    /// ```
    pub async fn search(&self, from: u64, to: u64, date: &str) -> Result<TripSearchResponse> {
        self.search_with(
            TripFilter::new(from, to, date),
            TripSearchOptions::default(),
        )
        .await
    }

    /// Search with full control over filter + options.
    ///
    /// The response is post-processed to match exactly what the VeXeRe website
    /// renders: see [`strip_hidden_points`].
    pub async fn search_with(
        &self,
        filter: TripFilter,
        opts: TripSearchOptions,
    ) -> Result<TripSearchResponse> {
        filter.validate()?;
        let url = self.build_url(&filter, opts)?;
        let mut resp: TripSearchResponse = self.client.get_internal(&url).await?;
        strip_hidden_points(&mut resp);
        Ok(resp)
    }

    /// Iterate **all** pages of a search concurrently (limited by the client's
    /// semaphore), returning a flat `Vec<Trip>`.
    ///
    /// First page is fetched to learn `total_pages`, then remaining pages are
    /// requested in parallel.
    pub async fn search_all_pages(&self, filter: TripFilter) -> Result<Vec<crate::models::Trip>> {
        filter.validate()?;
        // Page 1 first, to learn total_pages.
        let first = self
            .search_with(filter.clone(), TripSearchOptions::default())
            .await?;
        let total_pages = first.total_pages.max(1);
        if total_pages == 1 {
            return Ok(first.data);
        }
        // Parallel fetch remaining pages.
        let mut handles = Vec::with_capacity((total_pages - 1) as usize);
        for p in 2..=total_pages {
            let f = filter.clone();
            let c = self.client.clone();
            handles.push(tokio::spawn(async move {
                c.trips()
                    .search_with(f, TripSearchOptions::default().with_page(p))
                    .await
            }));
        }
        let mut out = first.data;
        for h in handles {
            let r = h
                .await
                .map_err(|e| Error::Internal(format!("join error: {e}")))?
                .map(|r| r.data)
                .unwrap_or_default();
            out.extend(r);
        }
        Ok(out)
    }

    fn build_url(&self, f: &TripFilter, o: TripSearchOptions) -> Result<Url> {
        let mut url: Url = self
            .client
            .endpoints()
            .trip_service
            .join("v2/route/")
            .map_err(|e| Error::Internal(format!("url build: {e}")))?;
        // We use the same `filter[k]=v` bracketed style VeXeRe expects.
        let mut q = url.query_pairs_mut();
        q.append_pair("filter[from]", &f.from.to_string());
        q.append_pair("filter[to]", &f.to.to_string());
        q.append_pair("filter[quantity]", &f.quantity.to_string());
        q.append_pair("filter[date]", &f.date);
        q.append_pair("page", &o.page.to_string());
        q.append_pair("pagesize", &o.page_size.to_string());
        // Sort: lowest price ascending (matches FE default).
        q.append_pair("sort", "asc");
        // Hint the server that we want it to trigger a fresh crawl for live seats.
        q.append_pair("is_trigger_crawl", "1");
        drop(q);
        Ok(url)
    }
}

/// Drop pickup/dropoff points the website does not show.
///
/// **Why this exists**: the `/v2/route/` search endpoint returns each
/// operator's *entire* point pool on every trip, regardless of route. For
/// example, on 2026-09-09 the trips of "Anh Huy Đất Cảng" (company 12847)
/// each carried 1,953 pickup points — the operator's pool across the whole
/// region — while route 7275428 (Hải Phòng → Hà Nội) actually serves exactly
/// **1** pickup point ("Bến xe Thượng Lý"). The VeXeRe website only renders
/// points whose top-level `hidden` flag is `0`; points with `hidden: 1` are
/// inactive for that route. Filtering here — at the single choke point every
/// search passes through — makes every downstream artifact (trips.jsonl,
/// fares, Excel, SQLite) match the website exactly.
///
/// Points missing the `hidden` field entirely are kept (treated as visible),
/// which matches the FE's tolerant rendering.
pub fn strip_hidden_points(resp: &mut TripSearchResponse) {
    for trip in &mut resp.data {
        trip.route.pickup_points.retain(|p| p.hidden == 0);
        trip.route.dropoff_points.retain(|p| p.hidden == 0);
    }
}
