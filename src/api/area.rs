//! `GET /v3/area` (autocomplete) and `GET /v1/goyolo/area/{id}` (detail).

use url::Url;

use crate::client::VexereClient;
use crate::error::Result;
use crate::models::{AreaDetail, AreaDetailResponse, AreaSearchResponse};

/// Handle for area-related endpoints.
#[derive(Clone)]
pub struct Area {
    client: VexereClient,
}

impl Area {
    pub(crate) fn new(client: VexereClient) -> Self {
        Self { client }
    }

    /// `GET /v3/area?q={query}&is_merged_province=1`
    ///
    /// Autocomplete search across provinces, districts, towns, and bus/train
    /// stations. Returns ranked matches.
    ///
    /// # Errors
    /// See [`crate::Error`].
    pub async fn search(&self, query: &str) -> Result<AreaSearchResponse> {
        let mut url: Url = self.client.endpoints().trip_service.join("v3/area")?;
        url.query_pairs_mut()
            .append_pair("q", query)
            .append_pair("is_merged_province", "1");
        self.client.get_internal(&url).await
    }

    /// `GET /v1/goyolo/area/{id}`
    ///
    /// Returns full detail for one area (province / district / town / station).
    ///
    /// # Errors
    /// See [`crate::Error`].
    pub async fn detail(&self, id: u64) -> Result<AreaDetail> {
        let url: Url = self
            .client
            .endpoints()
            .trip_service
            .join(&format!("v1/goyolo/area/{id}"))?;
        let resp: AreaDetailResponse = self.client.get_internal(&url).await?;
        Ok(resp.data)
    }
}
