//! API endpoint handles.
//!
//! Each sub-module wraps one `VeXeRe` microservice surface. The handles are
//! cheap clones of the parent [`VexereClient`](crate::VexereClient) — construct
//! them via [`VexereClient::areas`](crate::VexereClient::areas),
//! [`VexereClient::trips`](crate::VexereClient::trips), etc.

pub mod area;
pub mod auth;
pub mod counts;
pub mod home;
pub mod trips;

pub use area::Area;
pub use auth::Auth;
pub use counts::Counts;
pub use home::Home;
pub use trips::Trips;
