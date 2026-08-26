//! The core HTTP client: bearer-token auth, retry/backoff, bounded concurrency.

use std::sync::Arc;
use std::time::Duration;

use once_cell::sync::OnceCell;
use rand::Rng;
use reqwest::{header, Client, Method, StatusCode};
use tokio::sync::{Mutex, Semaphore};
use tracing::{debug, trace, warn};
use url::Url;

use crate::config::Config;
use crate::error::{Error, Result};

/// Anonymous token payload returned by `POST /getToken`.
#[derive(Debug, Clone, serde::Deserialize)]
pub(crate) struct AnonymousToken {
    /// `bearer`.
    #[allow(dead_code)]
    #[serde(rename = "token_type")]
    pub token_type: String,
    /// The JWT itself.
    #[serde(rename = "access_token")]
    pub access_token: String,
    /// Refresh token (unused — anonymous, no refresh).
    #[allow(dead_code)]
    #[serde(rename = "refresh_token")]
    pub refresh_token: String,
    /// Seconds until expiry.
    #[allow(dead_code)]
    #[serde(rename = "expires_in")]
    pub expires_in: u64,
}

/// Thread-safe cached bearer token.
type TokenCache = Arc<Mutex<Option<(String, std::time::Instant)>>>;

/// The entrypoint: a `VeXeRe` HTTP client with built-in bearer-token acquisition,
/// bounded concurrency, and exponential-backoff retries.
#[derive(Clone)]
pub struct VexereClient {
    cfg: Arc<Config>,
    http: Client,
    semaphore: Arc<Semaphore>,
    token: TokenCache,
    endpoints: Arc<crate::config::Endpoints>,
}

impl VexereClient {
    /// Construct a new client from the given [`Config`]. Performs no network I/O.
    ///
    /// # Errors
    /// Only fails if the underlying `reqwest::Client` fails to build (e.g. bad
    /// TLS backend).
    pub fn new(cfg: Config) -> Result<Self> {
        let http = Client::builder()
            .timeout(cfg.request_timeout)
            .pool_max_idle_per_host(cfg.concurrency.max_in_flight().max(8))
            .tcp_keepalive(Duration::from_secs(30))
            .user_agent(cfg.user_agent.clone())
            .brotli(true)
            .gzip(true)
            .build()?;

        let endpoints = Arc::new(cfg.endpoints.clone());
        let permits = cfg.concurrency.max_in_flight().max(1);
        let semaphore = Arc::new(Semaphore::new(permits));

        Ok(Self {
            cfg: Arc::new(cfg),
            http,
            semaphore,
            token: Arc::new(Mutex::new(None)),
            endpoints,
        })
    }

    /// Borrow the live [`Config`].
    #[must_use]
    pub fn config(&self) -> &Config {
        &self.cfg
    }

    /// Borrow the underlying `reqwest::Client` (escape hatch for callers).
    #[must_use]
    pub fn http(&self) -> &Client {
        &self.http
    }

    /// Returns an [`crate::api::Auth`] handle for token-related operations.
    #[must_use]
    pub fn auth(&self) -> crate::api::Auth {
        crate::api::Auth::new(self.clone())
    }

    /// Returns an [`crate::api::Area`] handle for area/autocomplete calls.
    #[must_use]
    pub fn areas(&self) -> crate::api::Area {
        crate::api::Area::new(self.clone())
    }

    /// Returns an [`crate::api::Trips`] handle for bus-trip search calls.
    #[must_use]
    pub fn trips(&self) -> crate::api::Trips {
        crate::api::Trips::new(self.clone())
    }

    /// Returns an [`crate::api::Counts`] handle for bus/flight/train count-by-month calls.
    #[must_use]
    pub fn counts(&self) -> crate::api::Counts {
        crate::api::Counts::new(self.clone())
    }

    /// Returns an [`crate::api::Home`] handle for homepage ASP.NET MVC endpoints.
    #[must_use]
    pub fn home(&self) -> crate::api::Home {
        crate::api::Home::new(self.clone())
    }

    /// Returns a reference to the configured endpoints.
    #[must_use]
    pub fn endpoints(&self) -> &crate::config::Endpoints {
        &self.endpoints
    }

    /// Acquire a (cached) bearer token, refreshing if missing or near-expiry.
    async fn bearer_token(&self) -> Result<String> {
        let mut guard = self.token.lock().await;
        if let Some((tok, fetched_at)) = guard.as_ref() {
            // Refresh ~5 min before expiry (tokens live 1h).
            if fetched_at.elapsed() < Duration::from_secs(3300) {
                return Ok(tok.clone());
            }
        }
        let token = self.fetch_token().await?;
        *guard = Some((token.access_token.clone(), std::time::Instant::now()));
        Ok(token.access_token)
    }

    /// Force-refresh the bearer token (e.g. after a 401).
    pub async fn refresh_token(&self) -> Result<()> {
        let token = self.fetch_token().await?;
        let mut guard = self.token.lock().await;
        *guard = Some((token.access_token, std::time::Instant::now()));
        Ok(())
    }

    /// POST /getToken (no body) → anonymous bearer token.
    async fn fetch_token(&self) -> Result<AnonymousToken> {
        let url = self.endpoints.base.join("/getToken").unwrap();
        let resp = self
            .http
            .post(url.as_str())
            .header(header::ACCEPT, "application/json, text/plain, */*")
            .header(header::ORIGIN, "https://vexere.com")
            .header(header::REFERER, "https://vexere.com/")
            .send()
            .await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Api {
                status: status.as_u16(),
                url: url.to_string(),
                body: truncate_body(&body),
            });
        }
        let token: AnonymousToken = resp.json().await?;
        debug!("fetched anonymous bearer token (expires in {}s)", token.expires_in);
        Ok(token)
    }

    /// Send a GET to an internal-vroute path with retries + concurrency.
    pub(crate) async fn get_internal<T: serde::de::DeserializeOwned>(
        &self,
        url: &Url,
    ) -> Result<T> {
        self.send_internal(Method::GET, url, None::<&serde_json::Value>)
            .await
    }

    /// Send a POST to an internal-vroute path with retries + concurrency.
    #[allow(dead_code)]
    pub(crate) async fn post_internal<T: serde::de::DeserializeOwned>(
        &self,
        url: &Url,
        body: &impl serde::Serialize,
    ) -> Result<T> {
        self.send_internal(Method::POST, url, Some(body)).await
    }

    /// Send a request to an internal-vroute endpoint with bearer token,
    /// bounded concurrency, jitter, retries with exponential backoff on 429/5xx.
    ///
    /// On success, deserializes the response body as `T`.
    async fn send_internal<T: serde::de::DeserializeOwned>(
        &self,
        method: Method,
        url: &Url,
        body: Option<&impl serde::Serialize>,
    ) -> Result<T> {
        let deadline = tokio::time::Instant::now() + self.cfg.call_deadline;
        let mut attempts = 0u32;
        loop {
            attempts += 1;
            // Concurrency: wait for a permit, with jitter.
            let _permit = self.acquire_permit_with_jitter().await;

            // Build the request (refreshes token if needed).
            let token = self.bearer_token().await?;
            let origin_request_id = generate_origin_request_id();
            let mut rb = self
                .http
                .request(method.clone(), url.as_str())
                .header(header::ACCEPT, "application/json, text/plain, */*")
                .header(header::ACCEPT_LANGUAGE, &self.cfg.accept_language)
                .header(header::AUTHORIZATION, format!("bearer {token}"))
                .header("origin-request-product", &self.cfg.origin_request_product)
                .header("origin-request-id", &origin_request_id)
                .header(header::ORIGIN, "https://vexere.com")
                .header(header::REFERER, "https://vexere.com/");

            if let Some(b) = body {
                rb = rb.json(b);
            }

            if self.cfg.trace_requests {
                trace!(?method, url = url.as_str(), attempt = attempts, "send");
            }

            let resp = rb.send().await;

            match resp {
                Ok(r) if r.status().is_success() => {
                    let parsed: T = r.json().await?;
                    return Ok(parsed);
                }
                Ok(r) => {
                    let status = r.status();
                    let body = r.text().await.unwrap_or_default();
                    if self.cfg.trace_requests {
                        debug!(
                            ?method,
                            url = url.as_str(),
                            status = status.as_u16(),
                            "non-2xx response"
                        );
                    }
                    if attempts <= self.cfg.max_retries
                        && is_retryable_status(status)
                        && tokio::time::Instant::now() < deadline
                    {
                        let backoff = self.backoff(attempts, status);
                        warn!(
                            attempt = attempts,
                            status = status.as_u16(),
                            backoff_ms = backoff.as_millis(),
                            "retrying"
                        );
                        tokio::time::sleep(backoff).await;
                        // On 401 specifically, refresh the token.
                        if status == StatusCode::UNAUTHORIZED {
                            self.refresh_token().await.ok();
                        }
                        continue;
                    }
                    return Err(Error::Api {
                        status: status.as_u16(),
                        url: url.to_string(),
                        body: truncate_body(&body),
                    });
                }
                Err(e) => {
                    if attempts <= self.cfg.max_retries
                        && (e.is_timeout() || e.is_connect())
                        && tokio::time::Instant::now() < deadline
                    {
                        let backoff = self.backoff(attempts, StatusCode::REQUEST_TIMEOUT);
                        warn!(
                            attempt = attempts,
                            err = %e,
                            backoff_ms = backoff.as_millis(),
                            "transport-level retry"
                        );
                        tokio::time::sleep(backoff).await;
                        continue;
                    }
                    return Err(Error::Http(e));
                }
            }
        }
    }

    /// Send a GET to a vexere.com MVC endpoint (no bearer token, just same-origin).
    #[allow(dead_code)]
    pub(crate) async fn get_mvc_text(&self, url: &Url) -> Result<String> {
        let deadline = tokio::time::Instant::now() + self.cfg.call_deadline;
        let mut attempts = 0u32;
        loop {
            attempts += 1;
            let _permit = self.acquire_permit_with_jitter().await;
            let resp = self
                .http
                .get(url.as_str())
                .header(header::ACCEPT, "application/json, text/html, */*")
                .header(header::ACCEPT_LANGUAGE, &self.cfg.accept_language)
                .header(header::REFERER, "https://vexere.com/")
                .send()
                .await?;
            let status = resp.status();
            if status.is_success() {
                return Ok(resp.text().await?);
            }
            if attempts <= self.cfg.max_retries
                && is_retryable_status(status)
                && tokio::time::Instant::now() < deadline
            {
                let backoff = self.backoff(attempts, status);
                warn!(
                    attempt = attempts,
                    status = status.as_u16(),
                    backoff_ms = backoff.as_millis(),
                    "retrying MVC endpoint"
                );
                tokio::time::sleep(backoff).await;
                continue;
            }
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Api {
                status: status.as_u16(),
                url: url.to_string(),
                body: truncate_body(&body),
            });
        }
    }

    /// Send a POST to a vexere.com MVC endpoint (returns text/html fragment).
    pub(crate) async fn post_mvc_text(
        &self,
        url: &Url,
        body: &impl serde::Serialize,
    ) -> Result<String> {
        let deadline = tokio::time::Instant::now() + self.cfg.call_deadline;
        let mut attempts = 0u32;
        loop {
            attempts += 1;
            let _permit = self.acquire_permit_with_jitter().await;
            let resp = self
                .http
                .post(url.as_str())
                .header(header::ACCEPT, "application/json, text/html, */*")
                .header(header::ACCEPT_LANGUAGE, &self.cfg.accept_language)
                .header(header::ORIGIN, "https://vexere.com")
                .header(header::REFERER, "https://vexere.com/")
                .json(body)
                .send()
                .await?;
            let status = resp.status();
            if status.is_success() {
                return Ok(resp.text().await?);
            }
            if attempts <= self.cfg.max_retries
                && is_retryable_status(status)
                && tokio::time::Instant::now() < deadline
            {
                let backoff = self.backoff(attempts, status);
                tokio::time::sleep(backoff).await;
                continue;
            }
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Api {
                status: status.as_u16(),
                url: url.to_string(),
                body: truncate_body(&body),
            });
        }
    }

    async fn acquire_permit_with_jitter(&self) -> tokio::sync::OwnedSemaphorePermit {
        let permit = self
            .semaphore
            .clone()
            .acquire_owned()
            .await
            .expect("semaphore closed");
        let jitter = self.cfg.concurrency.jitter();
        if !jitter.is_zero() {
            let max_ms = u64::try_from(jitter.as_millis()).unwrap_or(u64::MAX);
            let ms = rand::thread_rng().gen_range(0..=max_ms);
            tokio::time::sleep(Duration::from_millis(ms)).await;
        }
        permit
    }

    fn backoff(&self, attempts: u32, status: StatusCode) -> Duration {
        // 429 gets extra politeness.
        let extra = if status == StatusCode::TOO_MANY_REQUESTS {
            Duration::from_millis(500)
        } else {
            Duration::ZERO
        };
        let exp = 2u32.saturating_pow(attempts.saturating_sub(1).min(10));
        let base = self.cfg.backoff_base * exp;
        std::cmp::min(base + extra, self.cfg.backoff_max)
    }
}

fn is_retryable_status(s: StatusCode) -> bool {
    matches!(
        s.as_u16(),
        408 | 425 | 429 | 500 | 502 | 503 | 504
    )
}

fn truncate_body(s: &str) -> String {
    const MAX: usize = 2 * 1024;
    if s.len() <= MAX {
        s.to_string()
    } else {
        let mut t = s[..MAX].to_string();
        t.push_str("…[truncated]");
        t
    }
}

/// Generate an `origin-request-id` value matching the `VeXeRe` web client's format:
/// `vex-web_<unix_ms>_<6char>`
fn generate_origin_request_id() -> String {
    static COUNTER: OnceCell<std::sync::Mutex<u64>> = OnceCell::new();
    let c = COUNTER.get_or_init(|| std::sync::Mutex::new(0));
    let n = {
        let mut g = c.lock().unwrap();
        *g += 1;
        *g
    };
    let ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX));
    let rand_part: u64 = rand::thread_rng().gen_range(0..0xFFFF);
    let suffix = format!("{n}{rand_part:04X}");
    let suffix = if suffix.len() >= 6 {
        suffix[..6].to_string()
    } else {
        format!("{suffix:0>6}")
    };
    format!("vex-web_{ms}_{suffix}")
}


