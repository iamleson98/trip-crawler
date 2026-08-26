//! Configuration for [`crate::VexereClient`].

use std::time::Duration;
use url::Url;

// Re-export for the convenience of nested constants in this file.
// (no nested modules need to import Duration separately now)

/// Aggressive-concurrency preset chosen by the user (64+ in-flight requests).
pub mod concurrency_profile {
    use std::time::Duration;
    /// Polite (4–8 concurrent, 200 ms jitter).
    pub const POLITE: (usize, Duration) = (8, Duration::from_millis(200));
    /// Moderate (16–32 concurrent, 50 ms jitter).
    pub const MODERATE: (usize, Duration) = (32, Duration::from_millis(50));
    /// Aggressive (64 concurrent, no jitter except exponential backoff on 429/5xx).
    pub const AGGRESSIVE: (usize, Duration) = (64, Duration::from_millis(0));
}

/// Concurrency profile (max in-flight requests + per-request jitter).
#[derive(Clone, Copy, Debug, Default)]
pub enum ConcurrencyProfile {
    /// Polite: 8 concurrent, 200ms jitter.
    Polite,
    /// Moderate: 32 concurrent, 50ms jitter.
    Moderate,
    /// Aggressive: 64 concurrent, no jitter (default per user spec).
    #[default]
    Aggressive,
    /// Custom max + jitter.
    Custom(usize, Duration),
}

impl ConcurrencyProfile {
    /// Returns `(max_in_flight, jitter)`.
    #[must_use]
    pub fn params(self) -> (usize, Duration) {
        match self {
            Self::Polite => concurrency_profile::POLITE,
            Self::Moderate => concurrency_profile::MODERATE,
            Self::Aggressive => concurrency_profile::AGGRESSIVE,
            Self::Custom(n, d) => (n, d),
        }
    }

    /// Number of concurrent in-flight requests allowed.
    #[must_use]
    pub fn max_in_flight(self) -> usize {
        self.params().0
    }

    /// Per-request jitter applied before sending.
    #[must_use]
    pub fn jitter(self) -> Duration {
        self.params().1
    }
}

/// All base URLs used by the `VeXeRe` microservices.
///
/// Defaults point at the production hosts reverse-engineered from
/// `window.__NEXT_DATA__.runtimeConfig.URL` on the `VeXeRe` homepage.
#[derive(Clone, Debug)]
pub struct Endpoints {
    /// `https://vexere.com` — homepage + ASP.NET MVC surface.
    pub base: Url,
    /// `https://api.vexere.com` — main API gateway (mostly unused anonymous).
    pub main_api: Url,
    /// `https://internal-vroute.vexere.com` — *the* bus trip search API.
    pub trip_service: Url,
    /// `https://feconfig.vexere.com` — frontend config / hotlines.
    pub fe_config: Url,
    /// `https://seo-service.vexere.com` — SEO content (route-detail pages).
    pub seo: Url,
    /// `https://vconfiguration.vexere.com` — runtime configuration.
    pub configuration: Url,
    /// `https://vmaps.vexere.com` — maps (goong-backed).
    pub maps: Url,
    /// `https://vgate.vexere.com` — payment gateway (authed; not used anonymous).
    pub payment_gate: Url,
    /// `https://vtracking.vexere.com` — analytics (not used).
    pub tracking: Url,
    /// `https://account-service.vexere.com` — authed; not used.
    pub account: Url,
    /// `https://user-profile-service.vexere.com` — authed; not used.
    pub user_profile: Url,
}

impl Default for Endpoints {
    fn default() -> Self {
        // unwrap is safe — these are compile-time-checked literals.
        Self {
            base: Url::parse("https://vexere.com").unwrap(),
            main_api: Url::parse("https://api.vexere.com").unwrap(),
            trip_service: Url::parse("https://internal-vroute.vexere.com").unwrap(),
            fe_config: Url::parse("https://feconfig.vexere.com").unwrap(),
            seo: Url::parse("https://seo-service.vexere.com").unwrap(),
            configuration: Url::parse("https://vconfiguration.vexere.com").unwrap(),
            maps: Url::parse("https://vmaps.vexere.com").unwrap(),
            payment_gate: Url::parse("https://vgate.vexere.com").unwrap(),
            tracking: Url::parse("https://vtracking.vexere.com").unwrap(),
            account: Url::parse("https://account-service.vexere.com").unwrap(),
            user_profile: Url::parse("https://user-profile-service.vexere.com").unwrap(),
        }
    }
}

/// Top-level configuration. Build via [`Config::default`] or [`Config::builder`].
#[derive(Clone, Debug)]
pub struct Config {
    /// Microservice base URLs.
    pub endpoints: Endpoints,
    /// Concurrency profile.
    pub concurrency: ConcurrencyProfile,
    /// Per-request timeout.
    pub request_timeout: Duration,
    /// Total time budget for a single API call (including retries).
    pub call_deadline: Duration,
    /// User-Agent header value sent on every request.
    pub user_agent: String,
    /// Accept-Language header value sent on every request.
    pub accept_language: String,
    /// `origin-request-product` header value (`VeXeRe` telemetry).
    pub origin_request_product: String,
    /// Max retries per request (exponential backoff on 429/5xx).
    pub max_retries: u32,
    /// Base delay for exponential backoff.
    pub backoff_base: Duration,
    /// Maximum backoff cap.
    pub backoff_max: Duration,
    /// Whether to enable `tracing::` debug logs of every request.
    pub trace_requests: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            endpoints: Endpoints::default(),
            concurrency: ConcurrencyProfile::default(),
            request_timeout: Duration::from_secs(30),
            call_deadline: Duration::from_secs(120),
            user_agent: format!(
                "vexerers/{} (linux; rust) bot-crawler",
                env!("CARGO_PKG_VERSION")
            ),
            accept_language: "vi-VN".to_string(),
            origin_request_product: "vex-web".to_string(),
            max_retries: 5,
            backoff_base: Duration::from_millis(250),
            backoff_max: Duration::from_secs(8),
            trace_requests: false,
        }
    }
}

impl Config {
    /// Returns a fluent builder for [`Config`].
    #[must_use]
    pub fn builder() -> ConfigBuilder {
        ConfigBuilder::new()
    }
}

/// Fluent builder for [`Config`].
#[derive(Default)]
pub struct ConfigBuilder {
    inner: Config,
}

impl ConfigBuilder {
    /// Create a new builder with defaults.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Set the concurrency profile.
    #[must_use]
    pub fn concurrency(mut self, p: ConcurrencyProfile) -> Self {
        self.inner.concurrency = p;
        self
    }

    /// Override the request timeout.
    #[must_use]
    pub fn request_timeout(mut self, d: Duration) -> Self {
        self.inner.request_timeout = d;
        self
    }

    /// Override the User-Agent.
    #[must_use]
    pub fn user_agent(mut self, ua: impl Into<String>) -> Self {
        self.inner.user_agent = ua.into();
        self
    }

    /// Override the Accept-Language.
    #[must_use]
    pub fn accept_language(mut self, lang: impl Into<String>) -> Self {
        self.inner.accept_language = lang.into();
        self
    }

    /// Override max retries.
    #[must_use]
    pub fn max_retries(mut self, n: u32) -> Self {
        self.inner.max_retries = n;
        self
    }

    /// Toggle verbose request tracing.
    #[must_use]
    pub fn trace_requests(mut self, on: bool) -> Self {
        self.inner.trace_requests = on;
        self
    }

    /// Override the entire [`Endpoints`] struct.
    #[must_use]
    pub fn endpoints(mut self, e: Endpoints) -> Self {
        self.inner.endpoints = e;
        self
    }

    /// Build the final [`Config`].
    #[must_use]
    pub fn build(self) -> Config {
        self.inner
    }
}
