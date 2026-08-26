//! `vexere` — CLI for the [`vexerers`](../../doc/vexerers/index.html) crate.
//!
//! Subcommands:
//! - `search`     — one-shot trip search (from → to, date).
//! - `crawl-trips`— paginated trip crawl for a route + date range.
//! - `areas`      — area autocomplete search.
//! - `area`       — area detail by ID.
//! - `counts`     — bus/flight/train count for a route + date.
//! - `home`       — dump one of the `POST /vi-VN/Home/*` HTML fragments.
//! - `crawl-all`  — full crawl across popular routes + a date range → JSONL.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use time::Date;
use tracing_subscriber::EnvFilter;

use vexerers::{api::counts::CountKind, ConcurrencyProfile, Config, VexereClient};

/// Output format for crawl subcommands.
#[derive(Debug, Clone, Copy, clap::ValueEnum, PartialEq, Eq)]
enum FormatArg {
    /// `.jsonl` only (one JSON object per line).
    Jsonl,
    /// `.csv` only (header row + one row per record).
    Csv,
    /// Both `.jsonl` and `.csv`.
    Both,
}

impl FormatArg {
    fn to_sink_format(self) -> vexerers::SinkFormat {
        match self {
            Self::Jsonl => vexerers::SinkFormat::Jsonl,
            Self::Csv => vexerers::SinkFormat::Csv,
            Self::Both => vexerers::SinkFormat::Both,
        }
    }
}

#[derive(Debug, Parser)]
#[command(
    name = "vexere",
    version,
    about = "Reverse-engineered Rust client + crawler for the VeXeRe bus-trip API",
    long_about = "Anonymous-only: trip search, areas, schedules, counts. No booking or payment."
)]
struct Cli {
    /// Concurrency profile (default: aggressive = 64 in-flight).
    #[arg(long, value_enum, default_value_t = ProfileArg::Aggressive)]
    profile: ProfileArg,

    /// Max in-flight HTTP requests (overrides --profile if set).
    #[arg(long)]
    concurrency: Option<usize>,

    /// Per-request jitter (ms). 0 disables.
    #[arg(long, default_value_t = 0)]
    jitter: u64,

    /// Per-request timeout (seconds).
    #[arg(long, default_value_t = 30)]
    timeout: u64,

    /// Output format for crawl subcommands (default: both JSONL + CSV).
    #[arg(long, value_enum, default_value_t = FormatArg::Both)]
    format: FormatArg,

    /// Skip the flat `brands` and `fares` tables; write only raw `trips`.
    ///
    /// Useful when you only want the full nested trip JSON and will
    /// re-process downstream.
    #[arg(long = "raw-only")]
    raw_only: bool,

    /// Verbose: log every request.
    #[arg(long, short = 'v')]
    verbose: bool,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum ProfileArg {
    Polite,
    Moderate,
    Aggressive,
}

impl From<ProfileArg> for ConcurrencyProfile {
    fn from(p: ProfileArg) -> Self {
        match p {
            ProfileArg::Polite => Self::Polite,
            ProfileArg::Moderate => Self::Moderate,
            ProfileArg::Aggressive => Self::Aggressive,
        }
    }
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// One-shot bus trip search (paginated — pulls every page, prints summary).
    Search {
        /// From-area ID (e.g. 24 for Hà Nội).
        #[arg(long)]
        from: u64,
        /// To-area ID (e.g. 482 for Sa Pa).
        #[arg(long)]
        to: u64,
        /// Departure date in YYYY-MM-DD.
        #[arg(long)]
        date: String,
        /// Number of passengers.
        #[arg(long, default_value_t = 1)]
        quantity: u32,
        /// Print pretty JSON (default: NDJSON to stdout).
        #[arg(long)]
        pretty: bool,
    },

    /// Paginated trip crawl for a (route, date-range). Writes one JSONL line per trip.
    CrawlTrips {
        /// From-area ID.
        #[arg(long)]
        from: u64,
        /// To-area ID.
        #[arg(long)]
        to: u64,
        /// Start date (YYYY-MM-DD), inclusive.
        #[arg(long)]
        from_date: String,
        /// End date (YYYY-MM-DD), inclusive.
        #[arg(long)]
        to_date: String,
        /// Output directory (default: ./vexere-out).
        #[arg(long, default_value = "vexere-out")]
        out: PathBuf,
    },

    /// Area autocomplete search. Prints NDJSON.
    Areas {
        /// Search query (e.g. "Hà Nội" or "Sapa").
        #[arg(long, short = 'q')]
        query: String,
        /// Pretty JSON output.
        #[arg(long)]
        pretty: bool,
    },

    /// Area detail by ID.
    Area {
        /// Numeric area ID (e.g. 24).
        id: u64,
        /// Pretty JSON output.
        #[arg(long)]
        pretty: bool,
    },

    /// Bus / flight / train counts for a (route, date).
    Counts {
        /// Vehicle kind: bus, flight, train.
        #[arg(long, value_enum)]
        kind: KindArg,
        #[arg(long)]
        from: u64,
        #[arg(long)]
        to: u64,
        #[arg(long)]
        date: String,
    },

    /// Dump a `POST /vi-VN/Home/*` HTML fragment to stdout.
    Home {
        /// Which home endpoint.
        #[arg(value_enum)]
        endpoint: HomeArg,
    },

    /// Full crawl across popular routes + a date range → trips + brands + fares.
    CrawlAll {
        /// Start date (YYYY-MM-DD).
        #[arg(long)]
        from_date: String,
        /// End date (YYYY-MM-DD).
        #[arg(long)]
        to_date: String,
        /// Output directory (default: ./vexere-out).
        #[arg(long, default_value = "vexere-out")]
        out: PathBuf,
    },

    /// Print summary stats from an existing trips.jsonl file.
    ///
    /// Reads <dir>/trips.jsonl and prints: total trips, unique brands,
    /// price ranges (min / max / avg / median), total available seats.
    /// Also re-derives and writes brands.jsonl + fares.jsonl if absent
    /// (so you can run `summary` after a `--raw-only` crawl to backfill
    /// the flat tables).
    Summary {
        /// Directory containing trips.jsonl (and where brands.jsonl /
        /// fares.jsonl will be written if missing).
        #[arg(long, default_value = "vexere-out")]
        dir: PathBuf,

        /// Also write brands.jsonl + fares.jsonl even if they exist.
        #[arg(long)]
        force: bool,
    },

    /// Extract the `brands` (operator dimension) table from an existing
    /// `trips.jsonl`. Writes `brands.{jsonl,csv}` next to it.
    ///
    /// Useful when you've already crawled with `--raw-only` and now want
    /// the flat operator dimension table without re-running a full
    /// `summary`.
    Brands {
        /// Directory containing trips.jsonl.
        #[arg(long, default_value = "vexere-out")]
        dir: PathBuf,
        /// Overwrite brands.{jsonl,csv} even if they already exist.
        #[arg(long)]
        force: bool,
    },

    /// Extract the `fares` (price fact) table from an existing `trips.jsonl`.
    /// Writes `fares.{jsonl,csv}` next to it.
    ///
    /// One row per (trip, schedule) — every bookable departure with all
    /// price + seat info flattened.
    Fares {
        /// Directory containing trips.jsonl.
        #[arg(long, default_value = "vexere-out")]
        dir: PathBuf,
        /// Overwrite fares.{jsonl,csv} even if they already exist.
        #[arg(long)]
        force: bool,
    },

    /// Extract per-route aggregates from an existing `trips.jsonl`.
    /// Writes `routes.{jsonl,csv}` next to it and prints a table.
    ///
    /// One row per (from_area_id, to_area_id, date) — grain matches the
    /// `summaries` table from a live crawl, but derived from disk so you
    /// can run it on data crawled with `--raw-only`.
    Routes {
        /// Directory containing trips.jsonl.
        #[arg(long, default_value = "vexere-out")]
        dir: PathBuf,
        /// Overwrite routes.{jsonl,csv} even if they already exist.
        #[arg(long)]
        force: bool,
    },
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum KindArg {
    Bus,
    Flight,
    Train,
}

impl From<KindArg> for CountKind {
    fn from(k: KindArg) -> Self {
        match k {
            KindArg::Bus => Self::Bus,
            KindArg::Flight => Self::Flight,
            KindArg::Train => Self::Train,
        }
    }
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum HomeArg {
    PopularRoute,
    FeaturedBusInfo,
    FeaturedBusStopInfo,
    FeaturedLatestNews,
}

impl From<HomeArg> for vexerers::api::home::HomeEndpoint {
    fn from(h: HomeArg) -> Self {
        match h {
            HomeArg::PopularRoute => Self::PopularRoute,
            HomeArg::FeaturedBusInfo => Self::FeaturedBusInfo,
            HomeArg::FeaturedBusStopInfo => Self::FeaturedBusStopInfo,
            HomeArg::FeaturedLatestNews => Self::FeaturedLatestNews,
        }
    }
}

fn build_cli_config(cli: &Cli) -> Config {
    let mut b = Config::builder()
        .request_timeout(Duration::from_secs(cli.timeout))
        .trace_requests(cli.verbose);
    if let Some(n) = cli.concurrency {
        b = b.concurrency(ConcurrencyProfile::Custom(
            n,
            Duration::from_millis(cli.jitter),
        ));
    } else {
        b = b.concurrency(cli.profile.into());
    }
    b.build()
}

fn parse_date(s: &str) -> Result<Date> {
    static FMT: once_cell::sync::Lazy<Vec<time::format_description::BorrowedFormatItem<'static>>> =
        once_cell::sync::Lazy::new(|| {
            time::format_description::parse_borrowed::<2>("[year]-[month]-[day]")
                .expect("valid format string")
        });
    let d = Date::parse(s, &*FMT)
        .with_context(|| format!("invalid date {s:?}; expected YYYY-MM-DD"))?;
    Ok(d)
}

fn date_iter(start: Date, end: Date) -> impl Iterator<Item = Date> {
    (0..).scan(start, move |cur, _| {
        if *cur > end {
            None
        } else {
            let today = *cur;
            *cur = today.next_day().unwrap_or(today);
            Some(today)
        }
    })
}

fn format_date(d: Date) -> String {
    static FMT: once_cell::sync::Lazy<Vec<time::format_description::BorrowedFormatItem<'static>>> =
        once_cell::sync::Lazy::new(|| {
            time::format_description::parse_borrowed::<2>("[year]-[month]-[day]")
                .expect("valid format string")
        });
    d.format(&*FMT).unwrap_or_default()
}

#[tokio::main]
async fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_target(false)
        .init();

    let cli = Cli::parse();
    let cfg = build_cli_config(&cli);

    let client = match VexereClient::new(cfg) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("error: failed to build client: {e}");
            return ExitCode::from(2);
        }
    };

    let res: anyhow::Result<()> = match cli.cmd {
        Cmd::Search {
            from,
            to,
            date,
            quantity,
            pretty,
        } => run_search(&client, from, to, &date, quantity, pretty).await,
        Cmd::CrawlTrips {
            from,
            to,
            from_date,
            to_date,
            out,
        } => {
            run_crawl_trips(
                &client,
                from,
                to,
                &from_date,
                &to_date,
                out,
                cli.format.to_sink_format(),
                cli.raw_only,
            )
            .await
        }
        Cmd::Areas { query, pretty } => run_areas(&client, &query, pretty).await,
        Cmd::Area { id, pretty } => run_area(&client, id, pretty).await,
        Cmd::Counts {
            kind,
            from,
            to,
            date,
        } => run_counts(&client, kind.into(), from, to, &date).await,
        Cmd::Home { endpoint } => run_home(&client, endpoint.into()).await,
        Cmd::CrawlAll {
            from_date,
            to_date,
            out,
        } => {
            run_crawl_all(
                &client,
                &from_date,
                &to_date,
                out,
                cli.format.to_sink_format(),
                cli.raw_only,
            )
            .await
        }
        Cmd::Summary { dir, force } => run_summary(dir, force, cli.format.to_sink_format()).await,
        Cmd::Brands { dir, force } => run_brands(dir, force, cli.format.to_sink_format()).await,
        Cmd::Fares { dir, force } => run_fares(dir, force, cli.format.to_sink_format()).await,
        Cmd::Routes { dir, force } => run_routes(dir, force, cli.format.to_sink_format()).await,
    };

    if let Err(e) = res {
        eprintln!("error: {e}");
        let mut src = e.source();
        while let Some(s) = src {
            eprintln!("  caused by: {s}");
            src = s.source();
        }
        return ExitCode::from(1);
    }
    ExitCode::SUCCESS
}

async fn run_search(
    client: &VexereClient,
    from: u64,
    to: u64,
    date: &str,
    quantity: u32,
    pretty: bool,
) -> Result<()> {
    let filter = vexerers::api::trips::TripFilter::new(from, to, date).with_quantity(quantity);
    let resp = client
        .trips()
        .search_with(filter, vexerers::api::trips::TripSearchOptions::default())
        .await
        .context("search")?;
    eprintln!(
        "got {} trips (page {}/{})",
        resp.data.len(),
        resp.page,
        resp.total_pages
    );
    if pretty {
        println!("{}", serde_json::to_string_pretty(&resp)?);
    } else {
        println!("{}", serde_json::to_string(&resp)?);
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn run_crawl_trips(
    client: &VexereClient,
    from: u64,
    to: u64,
    from_date: &str,
    to_date: &str,
    out: PathBuf,
    fmt: vexerers::SinkFormat,
    raw_only: bool,
) -> Result<()> {
    let start = parse_date(from_date)?;
    let end = parse_date(to_date)?;
    if end < start {
        anyhow::bail!("to_date {to_date} is before from_date {from_date}");
    }
    let sink = std::sync::Arc::new(vexerers::DataSinkDir::new(&out));
    let mut total_written = 0usize;
    for d in date_iter(start, end) {
        let date = format_date(d);
        eprintln!("==> crawling {from}->{to} on {date}");
        let filter = vexerers::api::trips::TripFilter::new(from, to, &date);
        let trips = client.trips().search_all_pages(filter).await?;
        eprintln!("    got {} trips across all pages", trips.len());
        total_written += trips.len();
        write_trip_batch(&sink, "trips", &trips, from, to, &date, fmt, raw_only).await?;
    }
    eprintln!("==> done; wrote {total_written} trips to {}", out.display());
    Ok(())
}

async fn run_areas(client: &VexereClient, q: &str, pretty: bool) -> Result<()> {
    let resp = client.areas().search(q).await.context("areas")?;
    if pretty {
        println!("{}", serde_json::to_string_pretty(&resp)?);
    } else {
        for a in &resp.data {
            println!("{}", serde_json::to_string(a)?);
        }
    }
    Ok(())
}

async fn run_area(client: &VexereClient, id: u64, pretty: bool) -> Result<()> {
    let resp = client.areas().detail(id).await.context("area")?;
    if pretty {
        println!("{}", serde_json::to_string_pretty(&resp)?);
    } else {
        println!("{}", serde_json::to_string(&resp)?);
    }
    Ok(())
}

async fn run_counts(
    client: &VexereClient,
    kind: CountKind,
    from: u64,
    to: u64,
    date: &str,
) -> Result<()> {
    let req = vexerers::api::counts::CountRequest::new(from, to, date);
    let resp = client.counts().count(kind, &req).await.context("counts")?;
    println!("{}", serde_json::to_string_pretty(&resp)?);
    Ok(())
}

async fn run_home(
    client: &VexereClient,
    endpoint: vexerers::api::home::HomeEndpoint,
) -> Result<()> {
    let html = client.home().raw_html(endpoint).await.context("home")?;
    println!("{html}");
    Ok(())
}

/// A small built-in catalog of popular routes to seed `crawl-all`.
fn popular_routes() -> Vec<(u64, u64, &'static str)> {
    // (from_area_id, to_area_id, label)
    // IDs are from `GET /v3/area?q=...` (verified live).
    vec![
        (24, 482, "Hà Nội → Sa Pa (Lào Cai)"),
        (24, 50, "Hà Nội → Hồ Chí Minh"),
        (50, 24, "Hồ Chí Minh → Hà Nội"),
        (50, 23591, "Hồ Chí Minh → Đà Lạt"),
        (50, 91, "Hồ Chí Minh → Nha Trang"),
        (29, 482, "Hà Giang → Sa Pa"),
        (24, 53, "Hà Nội → Đà Nẵng"),
        (24, 417, "Hà Nội → Hải Phòng"),
        (50, 91, "Hồ Chí Minh → Nha Trang"),
        (24, 56, "Hà Nội → Huế"),
    ]
}

async fn run_crawl_all(
    client: &VexereClient,
    from_date: &str,
    to_date: &str,
    out: PathBuf,
    fmt: vexerers::SinkFormat,
    raw_only: bool,
) -> Result<()> {
    let start = parse_date(from_date)?;
    let end = parse_date(to_date)?;
    if end < start {
        anyhow::bail!("to_date {to_date} is before from_date {from_date}");
    }
    let sink = std::sync::Arc::new(vexerers::DataSinkDir::new(&out));

    // 1. Dump areas (seed catalog — collect unique IDs from popular_routes).
    let mut area_ids: std::collections::BTreeSet<u64> = std::collections::BTreeSet::new();
    for (from, to, _) in popular_routes() {
        area_ids.insert(from);
        area_ids.insert(to);
    }
    eprintln!("==> dumping area details for {} areas", area_ids.len());
    for id in area_ids {
        match client.areas().detail(id).await {
            Ok(a) => sink.write("areas", fmt, &a).await?,
            Err(e) => eprintln!("    WARN area {id}: {e}"),
        }
    }

    // 2. For each route × date, fetch all pages and write trips + brands + fares.
    let mut total = 0usize;
    for (from, to, label) in popular_routes() {
        for d in date_iter(start, end) {
            let date = format_date(d);
            eprintln!("==> {label} on {date}");
            let filter = vexerers::api::trips::TripFilter::new(from, to, &date);
            let trips = match client.trips().search_all_pages(filter).await {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("    WARN: {e}");
                    continue;
                }
            };
            eprintln!("    got {} trips", trips.len());
            total += trips.len();
            if let Err(e) =
                write_trip_batch(&sink, "trips", &trips, from, to, &date, fmt, raw_only).await
            {
                eprintln!("    WARN write: {e}");
            }
        }
    }
    eprintln!("==> done; wrote {total} trips to {}", out.display());
    Ok(())
}

/// Write one batch of trips + derived brands + fares to a [`DataSinkDir`].
///
/// `trips` is **always written as JSONL** (its `extra: serde_json::Value`
/// catch-all field can't be flattened to CSV columns). The flat `brands`,
/// `fares`, and `summaries` tables follow `fmt` (JSONL, CSV, or both).
///
/// Unless `raw_only`, derives the flat tables from each batch.
#[allow(clippy::too_many_arguments)]
async fn write_trip_batch(
    sink: &vexerers::DataSinkDir,
    _trips_name: &str,
    trips: &[vexerers::Trip],
    from: u64,
    to: u64,
    date: &str,
    fmt: vexerers::SinkFormat,
    raw_only: bool,
) -> Result<()> {
    // 1. Raw trips — JSONL only (nested shape can't go to CSV cleanly).
    sink.write_many("trips", vexerers::SinkFormat::Jsonl, trips)
        .await?;

    if raw_only {
        return Ok(());
    }

    // 2. Brands — one row per unique operator in this batch.
    let brands = vexerers::extract_brands(trips);
    if !brands.is_empty() {
        sink.write_many("brands", fmt, &brands).await?;
    }

    // 3. Fares — one row per (trip, schedule). The fact table.
    let fares = vexerers::extract_fares(trips, from, to, date);
    if !fares.is_empty() {
        sink.write_many("fares", fmt, &fares).await?;
    }

    // 4. Per-route summary — one row per (from, to, date).
    let summary = vexerers::summarize_route(trips, from, to, date);
    sink.write("summaries", fmt, &summary).await?;

    Ok(())
}

/// Read `<dir>/trips.jsonl`, summarize stats, and (optionally) backfill
/// `brands.jsonl` and `fares.jsonl` if missing.
///
/// Useful for inspecting a previously-crawled dataset, or for deriving the
/// flat tables from a `--raw-only` crawl after the fact.
async fn run_summary(dir: PathBuf, force: bool, fmt: vexerers::SinkFormat) -> Result<()> {
    let trips_path = dir.join("trips.jsonl");
    eprintln!("==> reading {}", trips_path.display());
    let all_trips = read_trips_jsonl(&trips_path)?;
    eprintln!("    parsed {} trips", all_trips.len());

    // Derive brands + fares + summaries.
    let brands = vexerers::extract_brands(&all_trips);
    let fares = vexerers::extract_fares(&all_trips, 0, 0, "unknown");
    eprintln!(
        "    derived {} brands, {} fare rows from {} trips",
        brands.len(),
        fares.len(),
        all_trips.len()
    );

    // Print summary to stdout — group by (from, to, date).
    let groups = group_trips_by_route_key(&all_trips);
    println!(
        "{:<14} {:<6} {:>10} {:>10} {:>12} {:>12} {:>12} {:>12}",
        "date", "route", "n_trips", "n_brands", "min_vnd", "median_vnd", "max_vnd", "avg_vnd"
    );
    println!("{:-<96}", "");
    for ((from, to, date), ts) in groups.iter() {
        let brands_for_date = vexerers::extract_brands(ts);
        let summary = vexerers::summarize_route(ts, *from, *to, date);
        let min_p = summary.min_price_vnd.unwrap_or(0);
        let max_p = summary.max_price_vnd.unwrap_or(0);
        let med_p = summary.median_price_vnd.unwrap_or(0);
        let avg_p = summary.avg_price_vnd.unwrap_or(0.0);
        let route = format!("{from}->{to}");
        println!(
            "{:<14} {:<6} {:>10} {:>10} {:>12} {:>12} {:>12} {:>12.0}",
            date,
            route,
            ts.len(),
            brands_for_date.len(),
            min_p,
            med_p,
            max_p,
            avg_p
        );
    }

    // Optionally backfill flat tables.
    let brands_path = dir.join("brands.jsonl");
    let fares_path = dir.join("fares.jsonl");
    if force || !brands_path.exists() {
        eprintln!(
            "==> writing {} brands to {}",
            brands.len(),
            brands_path.display()
        );
        let sink = vexerers::DataSinkDir::new(&dir);
        sink.write_many("brands", fmt, &brands).await?;
    }
    if force || !fares_path.exists() {
        eprintln!(
            "==> writing {} fare rows to {}",
            fares.len(),
            fares_path.display()
        );
        let sink = vexerers::DataSinkDir::new(&dir);
        sink.write_many("fares", fmt, &fares).await?;
    }

    Ok(())
}

/// Extract the `brands` (operator dimension) table from `<dir>/trips.jsonl`
/// and write `brands.{jsonl,csv}` next to it. Prints a brief summary.
async fn run_brands(dir: PathBuf, force: bool, fmt: vexerers::SinkFormat) -> Result<()> {
    let trips_path = dir.join("trips.jsonl");
    let brands_jsonl = dir.join("brands.jsonl");
    let brands_csv = dir.join("brands.csv");
    if !force && (brands_jsonl.exists() || brands_csv.exists()) {
        eprintln!(
            "==> brands table already exists in {}; pass --force to overwrite",
            dir.display()
        );
        return Ok(());
    }
    eprintln!("==> reading {}", trips_path.display());
    let trips = read_trips_jsonl(&trips_path)?;
    eprintln!("    parsed {} trips", trips.len());

    let brands = vexerers::extract_brands(&trips);
    eprintln!("    derived {} unique brands", brands.len());
    let sink = vexerers::DataSinkDir::new(&dir);
    let written = sink.write_many("brands", fmt, &brands).await?;
    eprintln!(
        "==> wrote {written} brand rows to {}/brands.{{{}}}",
        dir.display(),
        fmt_extensions(fmt)
    );
    println!("brands: {written} (in {})", dir.display());
    Ok(())
}

/// Extract the `fares` (price fact) table from `<dir>/trips.jsonl` and write
/// `fares.{jsonl,csv}` next to it. Prints a brief summary.
async fn run_fares(dir: PathBuf, force: bool, fmt: vexerers::SinkFormat) -> Result<()> {
    let trips_path = dir.join("trips.jsonl");
    let fares_jsonl = dir.join("fares.jsonl");
    let fares_csv = dir.join("fares.csv");
    if !force && (fares_jsonl.exists() || fares_csv.exists()) {
        eprintln!(
            "==> fares table already exists in {}; pass --force to overwrite",
            dir.display()
        );
        return Ok(());
    }
    eprintln!("==> reading {}", trips_path.display());
    let trips = read_trips_jsonl(&trips_path)?;
    eprintln!("    parsed {} trips", trips.len());

    // For each trip, parse (from, to, date) from id_index when possible, so
    // the emitted fares table carries the correct route + date stamps even
    // when extracted from a raw-only crawl (where the search-filter values
    // are no longer available). Falls back to (0, 0, "unknown") when the
    // id_index doesn't match the expected shape.
    let mut all_fares: Vec<vexerers::FareRow> = Vec::with_capacity(trips.len() * 2);
    for t in &trips {
        let (from, to, date) =
            extract_route_key_from_id_index(&t.id_index).unwrap_or((0, 0, "unknown".to_string()));
        let mut fares = vexerers::extract_fares(std::slice::from_ref(t), from, to, &date);
        all_fares.append(&mut fares);
    }
    eprintln!(
        "    derived {} fare rows from {} trips",
        all_fares.len(),
        trips.len()
    );

    let sink = vexerers::DataSinkDir::new(&dir);
    let written = sink.write_many("fares", fmt, &all_fares).await?;
    eprintln!(
        "==> wrote {written} fare rows to {}/fares.{{{}}}",
        dir.display(),
        fmt_extensions(fmt)
    );
    println!("fares: {written} (in {})", dir.display());
    Ok(())
}

/// Aggregate per-route (from, to, date) summaries from `<dir>/trips.jsonl`,
/// write `routes.{jsonl,csv}` next to it, and print a brief table.
///
/// Grain matches `summaries` from a live crawl, but derived from disk so
/// you can run it on a `--raw-only` dataset.
async fn run_routes(dir: PathBuf, force: bool, fmt: vexerers::SinkFormat) -> Result<()> {
    let trips_path = dir.join("trips.jsonl");
    let routes_jsonl = dir.join("routes.jsonl");
    let routes_csv = dir.join("routes.csv");
    if !force && (routes_jsonl.exists() || routes_csv.exists()) {
        eprintln!(
            "==> routes table already exists in {}; pass --force to overwrite",
            dir.display()
        );
        return Ok(());
    }
    eprintln!("==> reading {}", trips_path.display());
    let trips = read_trips_jsonl(&trips_path)?;
    eprintln!("    parsed {} trips", trips.len());

    let groups = group_trips_by_route_key(&trips);
    let summaries: Vec<vexerers::RouteSummary> = groups
        .iter()
        .map(|((from, to, date), ts)| vexerers::summarize_route(ts, *from, *to, date))
        .collect();
    eprintln!("    derived {} route-day rows", summaries.len());

    // Print table to stdout.
    println!(
        "{:<6} {:<6} {:<12} {:>8} {:>10} {:>10} {:>12} {:>12} {:>12} {:>10}",
        "from",
        "to",
        "date",
        "n_trips",
        "n_sched",
        "n_brands",
        "min_vnd",
        "med_vnd",
        "max_vnd",
        "seats"
    );
    println!("{:-<110}", "");
    for s in &summaries {
        println!(
            "{:<6} {:<6} {:<12} {:>8} {:>10} {:>10} {:>12} {:>12} {:>12} {:>10}",
            s.from_area_id,
            s.to_area_id,
            s.date,
            s.n_trips,
            s.n_schedules,
            s.n_operators,
            s.min_price_vnd.unwrap_or(0),
            s.median_price_vnd.unwrap_or(0),
            s.max_price_vnd.unwrap_or(0),
            s.total_available_seats,
        );
    }

    let sink = vexerers::DataSinkDir::new(&dir);
    let written = sink.write_many("routes", fmt, &summaries).await?;
    eprintln!(
        "==> wrote {written} route rows to {}/routes.{{{}}}",
        dir.display(),
        fmt_extensions(fmt)
    );
    println!("routes: {written} (in {})", dir.display());
    Ok(())
}

// ---------------------------------------------------------------------------
// Shared helpers (used by `summary`, `brands`, `fares`, `routes`).
// ---------------------------------------------------------------------------

/// Read a `trips.jsonl` file into a `Vec<Trip>`. Lines that fail to parse
/// are counted as bad (up to 5 warnings printed), then skipped — does not
/// abort the whole read.
fn read_trips_jsonl(path: &std::path::Path) -> Result<Vec<vexerers::Trip>> {
    use std::io::BufRead;
    if !path.exists() {
        anyhow::bail!(
            "no trips.jsonl found at {}; run `vexere crawl-trips` first",
            path.display()
        );
    }
    let f = std::fs::File::open(path).with_context(|| format!("open {}", path.display()))?;
    let reader = std::io::BufReader::new(f);
    let mut trips: Vec<vexerers::Trip> = Vec::new();
    let mut n_lines = 0usize;
    let mut n_bad = 0usize;
    for line in reader.lines() {
        let line = line.context("read line")?;
        if line.trim().is_empty() {
            continue;
        }
        n_lines += 1;
        match serde_json::from_str::<vexerers::Trip>(&line) {
            Ok(t) => trips.push(t),
            Err(e) => {
                n_bad += 1;
                if n_bad <= 5 {
                    eprintln!("    WARN parse line {n_lines}: {e}");
                }
            }
        }
    }
    eprintln!(
        "    parsed {}/{} lines ({} bad)",
        trips.len(),
        n_lines,
        n_bad
    );
    Ok(trips)
}

/// Group a flat trip list by `(from_area_id, to_area_id, date)` parsed from
/// each trip's `id_index`. Trips whose `id_index` doesn't match the
/// expected shape fall back to `(0, 0, "unknown")` — they're still
/// grouped, just under the catch-all key.
fn group_trips_by_route_key(
    trips: &[vexerers::Trip],
) -> std::collections::BTreeMap<(u64, u64, String), Vec<vexerers::Trip>> {
    let mut out: std::collections::BTreeMap<(u64, u64, String), Vec<vexerers::Trip>> =
        std::collections::BTreeMap::new();
    for t in trips {
        let key =
            extract_route_key_from_id_index(&t.id_index).unwrap_or((0, 0, "unknown".to_string()));
        out.entry(key).or_default().push(t.clone());
    }
    out
}

/// Parse a trip `id_index` like `"351932250_2026-09-01_23:00_24_38"` into
/// `(from_area_id, to_area_id, date)`.
///
/// The `id_index` shape is `"<route_id>_<YYYY-MM-DD>_<HH:MM>_<from>_<to>"`.
/// Returns `None` if the string doesn't have at least 5 underscore-separated
/// segments, or if the last two don't parse as `u64`.
fn extract_route_key_from_id_index(id: &str) -> Option<(u64, u64, String)> {
    let parts: Vec<&str> = id.split('_').collect();
    if parts.len() < 5 {
        return None;
    }
    let date_candidate = parts[1];
    let is_date = date_candidate.len() == 10
        && date_candidate.as_bytes()[4] == b'-'
        && date_candidate.as_bytes()[7] == b'-';
    if !is_date {
        return None;
    }
    // The last two segments may have leading zeros; u64::parse handles that.
    let from: u64 = parts[parts.len() - 2].parse().ok()?;
    let to: u64 = parts[parts.len() - 1].parse().ok()?;
    Some((from, to, date_candidate.to_string()))
}

/// Extract the date (YYYY-MM-DD) from a trip `id_index` like
/// "351932250_2026-09-01_23:00_24_38". Returns `None` if the format doesn't match.
#[allow(dead_code)]
fn extract_date_from_id_index(id: &str) -> Option<String> {
    extract_route_key_from_id_index(id).map(|(_, _, d)| d)
}

/// Human-readable extension list for a `SinkFormat` (used in log lines).
fn fmt_extensions(fmt: vexerers::SinkFormat) -> &'static str {
    match fmt {
        vexerers::SinkFormat::Jsonl => "jsonl",
        vexerers::SinkFormat::Csv => "csv",
        vexerers::SinkFormat::Both => "jsonl,csv",
    }
}

#[cfg(test)]
mod cli_tests {
    use super::*;

    #[test]
    fn parses_route_key_from_canonical_id_index() {
        let (from, to, date) =
            extract_route_key_from_id_index("351932250_2026-09-01_23:00_24_38").unwrap();
        assert_eq!(from, 24);
        assert_eq!(to, 38);
        assert_eq!(date, "2026-09-01");
    }

    #[test]
    fn route_key_returns_none_for_malformed_id() {
        assert!(extract_route_key_from_id_index("garbage").is_none());
        assert!(extract_route_key_from_id_index("a_b_c_d").is_none()); // 4 segments
                                                                       // 5 segments but date doesn't match shape:
        assert!(extract_route_key_from_id_index("1_not_a_date_23:00_24_38").is_none());
        // 5 segments but last two aren't numeric:
        assert!(extract_route_key_from_id_index("1_2026-09-01_23:00_x_38").is_none());
    }

    #[test]
    fn route_key_returns_none_when_last_segments_not_numeric() {
        // 6 segments here — last is "extra_segment" which doesn't parse as u64.
        assert!(
            extract_route_key_from_id_index("100_2026-09-01_23:00_24_482_extra_segment").is_none()
        );
    }

    #[test]
    fn fmt_extensions_maps_variants() {
        assert_eq!(fmt_extensions(vexerers::SinkFormat::Jsonl), "jsonl");
        assert_eq!(fmt_extensions(vexerers::SinkFormat::Csv), "csv");
        assert_eq!(fmt_extensions(vexerers::SinkFormat::Both), "jsonl,csv");
    }
}
