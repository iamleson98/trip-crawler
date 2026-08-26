//! Error types for the crate.

use thiserror::Error;

/// Unified error type returned by all fallible operations in this crate.
#[derive(Debug, Error)]
pub enum Error {
    /// HTTP transport error (connection refused, TLS, DNS, timeout).
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),

    /// Server returned non-2xx status.
    #[error("vexere api error: {status} {url} — {body}")]
    Api {
        /// HTTP status code returned by the server.
        status: u16,
        /// URL of the failed request.
        url: String,
        /// Truncated response body (≤ 2 KiB) for diagnostics.
        body: String,
    },

    /// JSON (de)serialization failed.
    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    /// CSV (de)serialization failed.
    #[error("csv error: {0}")]
    Csv(#[from] csv::Error),

    /// URL parsing/building failed.
    #[error("url error: {0}")]
    Url(#[from] url::ParseError),

    /// I/O error (file system, JSONL sink, etc).
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// All retries exhausted.
    #[error("retries exhausted after {attempts} attempts; last status: {last_status}")]
    RetriesExhausted {
        /// Number of attempts made.
        attempts: u32,
        /// Last HTTP status received (0 if transport-level failure).
        last_status: u16,
    },

    /// Invalid argument passed by caller.
    #[error("invalid argument: {0}")]
    InvalidArgument(String),

    /// Catch-all for unexpected conditions.
    #[error("internal error: {0}")]
    Internal(String),
}

/// Convenience type alias used everywhere in the crate.
pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    /// Returns the HTTP status code if this is an [`Error::Api`] or
    /// [`Error::RetriesExhausted`], else `0`.
    #[must_use]
    pub fn status_code(&self) -> u16 {
        match self {
            Self::Api { status, .. } => *status,
            Self::RetriesExhausted { last_status, .. } => *last_status,
            _ => 0,
        }
    }

    /// `true` if the error represents a transient (retryable) condition.
    #[must_use]
    pub fn is_retryable(&self) -> bool {
        match self {
            Self::Http(e) => e.is_timeout() || e.is_connect() || e.is_request(),
            Self::Api { status, .. } => matches!(status, &(429 | 500..=599)),
            Self::RetriesExhausted { .. } => true,
            _ => false,
        }
    }
}
