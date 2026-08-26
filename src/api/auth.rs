//! `Auth` handle for force-refreshing the anonymous bearer token.

use crate::client::VexereClient;

/// Handle for token-related operations.
///
/// Construct via [`crate::VexereClient::auth`].
#[derive(Clone)]
pub struct Auth {
    client: VexereClient,
}

impl Auth {
    pub(crate) fn new(client: VexereClient) -> Self {
        Self { client }
    }

    /// Force a token refresh now.
    pub async fn refresh(&self) -> crate::Result<()> {
        self.client.refresh_token().await
    }
}
