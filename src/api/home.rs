//! `POST /vi-VN/Home/{PopularRoute, FeaturedBusInfo, FeaturedBusStopInfo, FeaturedLatestNews}`
//!
//! These are ASP.NET MVC endpoints that return *HTML fragments*, not JSON.
//! They're still useful for crawling the homepage feed. We return the raw HTML
//! and let the caller parse with their preferred HTML parser.

use serde::{Deserialize, Serialize};
use url::Url;

use crate::client::VexereClient;
use crate::error::Result;

/// Which home endpoint to hit.
#[derive(Debug, Clone, Copy)]
pub enum HomeEndpoint {
    /// `POST /vi-VN/Home/PopularRoute` — popular routes from/to specific cities.
    PopularRoute,
    /// `POST /vi-VN/Home/FeaturedBusInfo` — featured bus operators.
    FeaturedBusInfo,
    /// `POST /vi-VN/Home/FeaturedBusStopInfo` — featured bus stations.
    FeaturedBusStopInfo,
    /// `POST /vi-VN/Home/FeaturedLatestNews` — latest news.
    FeaturedLatestNews,
}

impl HomeEndpoint {
    fn path(self) -> &'static str {
        match self {
            Self::PopularRoute => "vi-VN/Home/PopularRoute",
            Self::FeaturedBusInfo => "vi-VN/Home/FeaturedBusInfo",
            Self::FeaturedBusStopInfo => "vi-VN/Home/FeaturedBusStopInfo",
            Self::FeaturedLatestNews => "vi-VN/Home/FeaturedLatestNews",
        }
    }
}

/// Empty body (the endpoint accepts `{}`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HomeBody {}

/// Handle for `/vi-VN/Home/*` ASP.NET MVC endpoints.
#[derive(Clone)]
pub struct Home {
    client: VexereClient,
}

impl Home {
    pub(crate) fn new(client: VexereClient) -> Self {
        Self { client }
    }

    /// POST an empty body to a `/vi-VN/Home/*` endpoint and return the raw HTML fragment.
    ///
    /// # Errors
    /// See [`crate::Error`].
    pub async fn raw_html(&self, endpoint: HomeEndpoint) -> Result<String> {
        let url: Url = self.client.endpoints().base.join(endpoint.path())?;
        self.client.post_mvc_text(&url, &HomeBody {}).await
    }

    /// Convenience: popular routes HTML.
    pub async fn popular_routes(&self) -> Result<String> {
        self.raw_html(HomeEndpoint::PopularRoute).await
    }

    /// Convenience: featured bus info HTML.
    pub async fn featured_bus(&self) -> Result<String> {
        self.raw_html(HomeEndpoint::FeaturedBusInfo).await
    }

    /// Convenience: featured bus stops HTML.
    pub async fn featured_bus_stops(&self) -> Result<String> {
        self.raw_html(HomeEndpoint::FeaturedBusStopInfo).await
    }

    /// Convenience: latest news HTML.
    pub async fn latest_news(&self) -> Result<String> {
        self.raw_html(HomeEndpoint::FeaturedLatestNews).await
    }
}
