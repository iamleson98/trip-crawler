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
    if !trips_path.exists() {
        anyhow::bail!(
            "no trips.jsonl found in {}; run `vexere crawl-trips` first",
            dir.display()
        );
    }
    eprintln!("==> reading {}", trips_path.display());

    // Parse trips.jsonl line by line, accumulating trips + fares + brands.
    use std::io::BufRead;
    let f = std::fs::File::open(&trips_path).context("open trips.jsonl")?;
    let reader = std::io::BufReader::new(f);
    let mut all_trips: Vec<vexerers::Trip> = Vec::new();
    let mut n_lines = 0usize;
    let mut n_bad = 0usize;
    for line in reader.lines() {
        let line = line.context("read line")?;
        if line.trim().is_empty() {
            continue;
        }
        n_lines += 1;
        match serde_json::from_str::<vexerers::Trip>(&line) {
            Ok(t) => all_trips.push(t),
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
        all_trips.len(),
        n_lines,
        n_bad
    );

    // Derive brands + fares + summaries.
    let brands = vexerers::extract_brands(&all_trips);
    let fares = vexerers::extract_fares(&all_trips, 0, 0, "unknown");
    eprintln!(
        "    derived {} brands, {} fare rows from {} trips",
        brands.len(),
        fares.len(),
        all_trips.len()
    );

    // Print summary to stderr — group by date.
    let mut by_date: std::collections::BTreeMap<String, Vec<vexerers::Trip>> =
        std::collections::BTreeMap::new();
    for t in &all_trips {
        // Try to extract the date from the trip id_index ("..._YYYY-MM-DD_...").
        let date = extract_date_from_id_index(&t.id_index).unwrap_or_else(|| "unknown".to_string());
        by_date.entry(date).or_default().push(t.clone());
    }
    println!(
        "{:<12} {:>10} {:>10} {:>12} {:>12} {:>12} {:>12}",
        "date", "n_trips", "n_brands", "min_vnd", "median_vnd", "max_vnd", "avg_vnd"
    );
    println!("{:-<90}", "");
    for (date, ts) in by_date.iter() {
        let brands_for_date = vexerers::extract_brands(ts);
        let fares_for_date = vexerers::extract_fares(ts, 0, 0, date);
        let summary = vexerers::summarize_route(ts, 0, 0, date);
        let n_brands = brands_for_date.len();
        let _ = fares_for_date; // unused but kept for clarity
        let _ = &summary;
        let min_p = summary.min_price_vnd.unwrap_or(0);
        let max_p = summary.max_price_vnd.unwrap_or(0);
        let med_p = summary.median_price_vnd.unwrap_or(0);
        let avg_p = summary.avg_price_vnd.unwrap_or(0.0);
        println!(
            "{:<12} {:>10} {:>10} {:>12} {:>12} {:>12} {:>12.0}",
            date,
            ts.len(),
            n_brands,
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

/// Extract the date (YYYY-MM-DD) from a trip `id_index` like
/// "351932250_2026-09-01_23:00_24_38". Returns `None` if the format doesn't match.
fn extract_date_from_id_index(id: &str) -> Option<String> {
    // id_index has the shape "<route_id>_<YYYY-MM-DD>_<HH:MM>_<from>_<to>".
    // The date is the 2nd underscore-separated segment.
    let parts: Vec<&str> = id.split('_').collect();
    if parts.len() < 2 {
        return None;
    }
    let candidate = parts[1];
    if candidate.len() == 10 && candidate.as_bytes()[4] == b'-' && candidate.as_bytes()[7] == b'-' {
        Some(candidate.to_string())
    } else {
        None
    }
}
