//! Append-only sinks with crash-safe per-line flushes.
//!
//! Each entity type (trips, brands, fares, areas, …) gets its own file under
//! a chosen directory. Writes are line-buffered and flushed after every batch
//! so crawler progress survives a crash mid-run.
//!
//! Two formats are supported:
//! - **JSONL** — one JSON object per line. Preserves the full nested shape of
//!   each record. Use for streaming / NoSQL ingestion.
//! - **CSV** — one row per record, header row first. Use for spreadsheets
//!   / SQL `COPY` ingestion / BI tools.
//!
//! The same `DataSinkDir` can write both formats side by side (e.g.
//! `trips.jsonl` and `trips.csv` in the same dir).

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;
use tokio::sync::Mutex;

use crate::error::Result;

// ---------------------------------------------------------------------------
// JSONL sink — one JSON object per line.
// ---------------------------------------------------------------------------

/// Append-only JSONL writer for one entity type.
pub struct JsonlSink {
    file: Mutex<BufWriter<File>>,
    path: PathBuf,
}

impl JsonlSink {
    /// Open (or create) a JSONL file at `path` in append mode.
    ///
    /// The parent directory is created if missing.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        Ok(Self {
            file: Mutex::new(BufWriter::new(file)),
            path,
        })
    }

    /// Append one record (serialized as one JSON line).
    pub async fn write<T: Serialize + Send>(&self, record: &T) -> Result<()> {
        let mut line = serde_json::to_vec(record)?;
        line.push(b'\n');
        let mut w = self.file.lock().await;
        w.write_all(&line)?;
        w.flush()?;
        Ok(())
    }

    /// Append many records sequentially.
    pub async fn write_many<T: Serialize + Send>(&self, records: &[T]) -> Result<usize> {
        let mut w = self.file.lock().await;
        let mut n = 0;
        for r in records {
            serde_json::to_writer(&mut *w, r)?;
            w.write_all(b"\n")?;
            n += 1;
        }
        w.flush()?;
        Ok(n)
    }

    /// Path of the underlying file.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

// ---------------------------------------------------------------------------
// CSV sink — header row + one row per record.
// ---------------------------------------------------------------------------

/// Append-only CSV writer for one entity type.
///
/// The CSV header is auto-derived from the field names of the first record
/// serialized (the `csv` crate handles this). Subsequent records must share
/// the same shape — mixing schemas is undefined.
pub struct CsvSink {
    inner: Mutex<csv::Writer<BufWriter<File>>>,
    path: PathBuf,
}

impl CsvSink {
    /// Open (or create) a CSV file at `path` in append mode.
    ///
    /// The parent directory is created if missing. If the file already
    /// exists with content, the header row is **not** re-written on append
    /// (so it's safe to resume a crashed crawl).
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let already_has_content = path.exists() && std::fs::metadata(&path)?.len() > 0;
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let buf = BufWriter::new(file);
        let mut builder = csv::WriterBuilder::new();
        // If we're appending to an existing file, don't write headers again.
        builder.has_headers(!already_has_content);
        let writer = builder.from_writer(buf);
        Ok(Self {
            inner: Mutex::new(writer),
            path,
        })
    }

    /// Append one record (serialized as one CSV row).
    pub async fn write<T: Serialize + Send>(&self, record: &T) -> Result<()> {
        let mut w = self.inner.lock().await;
        w.serialize(record)?;
        w.flush()?;
        Ok(())
    }

    /// Append many records sequentially.
    pub async fn write_many<T: Serialize + Send>(&self, records: &[T]) -> Result<usize> {
        let mut w = self.inner.lock().await;
        let mut n = 0;
        for r in records {
            w.serialize(r)?;
            n += 1;
        }
        w.flush()?;
        Ok(n)
    }

    /// Path of the underlying file.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

// ---------------------------------------------------------------------------
// Combined directory: lazily opens JSONL + CSV sinks per entity name.
// ---------------------------------------------------------------------------

/// A directory of sinks, one per entity type + format. Sinks are lazily
/// opened on first write so unused entity files aren't created.
pub struct DataSinkDir {
    root: PathBuf,
    jsonl: Arc<Mutex<HashMap<String, Arc<JsonlSink>>>>,
    csv: Arc<Mutex<HashMap<String, Arc<CsvSink>>>>,
}

/// Output format selector.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SinkFormat {
    /// `.jsonl` only (default for the legacy `JsonlSinkDir` API).
    #[default]
    Jsonl,
    /// `.csv` only.
    Csv,
    /// Both `.jsonl` and `.csv`.
    Both,
}

impl DataSinkDir {
    /// Create a sink directory rooted at `root`. The directory itself is
    /// created lazily on first write.
    #[must_use]
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self {
            root: root.as_ref().to_path_buf(),
            jsonl: Arc::new(Mutex::new(HashMap::new())),
            csv: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Append one record to `<root>/<name>.{jsonl,csv}` according to `fmt`.
    pub async fn write<T: Serialize + Send>(
        &self,
        name: &str,
        fmt: SinkFormat,
        record: &T,
    ) -> Result<()> {
        if matches!(fmt, SinkFormat::Jsonl | SinkFormat::Both) {
            let sink = self.get_or_open_jsonl(name).await?;
            sink.write(record).await?;
        }
        if matches!(fmt, SinkFormat::Csv | SinkFormat::Both) {
            let sink = self.get_or_open_csv(name).await?;
            sink.write(record).await?;
        }
        Ok(())
    }

    /// Append many records at once to `<root>/<name>.{jsonl,csv}` per `fmt`.
    pub async fn write_many<T: Serialize + Send>(
        &self,
        name: &str,
        fmt: SinkFormat,
        records: &[T],
    ) -> Result<usize> {
        if records.is_empty() {
            return Ok(0);
        }
        let mut n = 0;
        if matches!(fmt, SinkFormat::Jsonl | SinkFormat::Both) {
            let sink = self.get_or_open_jsonl(name).await?;
            n = sink.write_many(records).await?;
        }
        if matches!(fmt, SinkFormat::Csv | SinkFormat::Both) {
            let sink = self.get_or_open_csv(name).await?;
            let m = sink.write_many(records).await?;
            // Both should match — pick whichever was actually written.
            n = n.max(m);
        }
        Ok(n)
    }

    async fn get_or_open_jsonl(&self, name: &str) -> Result<Arc<JsonlSink>> {
        let mut map = self.jsonl.lock().await;
        if let Some(s) = map.get(name) {
            return Ok(Arc::clone(s));
        }
        let path = self.root.join(format!("{name}.jsonl"));
        let sink = Arc::new(JsonlSink::open(path)?);
        map.insert(name.to_string(), Arc::clone(&sink));
        Ok(sink)
    }

    async fn get_or_open_csv(&self, name: &str) -> Result<Arc<CsvSink>> {
        let mut map = self.csv.lock().await;
        if let Some(s) = map.get(name) {
            return Ok(Arc::clone(s));
        }
        let path = self.root.join(format!("{name}.csv"));
        let sink = Arc::new(CsvSink::open(path)?);
        map.insert(name.to_string(), Arc::clone(&sink));
        Ok(sink)
    }
}

// ---------------------------------------------------------------------------
// Backward-compat shim: `JsonlSinkDir` == `DataSinkDir` pinned to JSONL.
// ---------------------------------------------------------------------------

/// Legacy alias for [`DataSinkDir`] pinned to JSONL output. Existing code
/// using `JsonlSinkDir::new(...).write(...)` continues to work unchanged.
pub struct JsonlSinkDir(DataSinkDir);

impl JsonlSinkDir {
    /// Create a JSONL-only sink directory. See [`DataSinkDir::new`].
    #[must_use]
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self(DataSinkDir::new(root))
    }

    /// Append one record to `<root>/<name>.jsonl`.
    pub async fn write<T: Serialize + Send>(&self, name: &str, record: &T) -> Result<()> {
        self.0.write(name, SinkFormat::Jsonl, record).await
    }

    /// Append many records at once to `<root>/<name>.jsonl`.
    pub async fn write_many<T: Serialize + Send>(
        &self,
        name: &str,
        records: &[T],
    ) -> Result<usize> {
        self.0.write_many(name, SinkFormat::Jsonl, records).await
    }

    /// Upcast to the full [`DataSinkDir`] for callers that want to mix formats.
    #[must_use]
    pub fn into_full(self) -> DataSinkDir {
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;

    #[derive(Serialize)]
    struct Row {
        id: i64,
        name: String,
        price: i64,
    }

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("vexerers-sink-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_file(&p);
        p
    }

    #[tokio::test]
    async fn csv_sink_writes_header_then_rows() {
        let path = tmp("header");
        let sink = CsvSink::open(&path).unwrap();
        sink.write(&Row {
            id: 1,
            name: "a".into(),
            price: 100,
        })
        .await
        .unwrap();
        sink.write(&Row {
            id: 2,
            name: "b".into(),
            price: 200,
        })
        .await
        .unwrap();
        drop(sink);

        let content = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = content.lines().filter(|l| !l.is_empty()).collect();
        assert_eq!(lines.len(), 3, "header + 2 data rows");
        assert!(lines[0].contains("id") && lines[0].contains("name") && lines[0].contains("price"));
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn csv_sink_does_not_rewrite_header_on_append() {
        let path = tmp("append");
        {
            let sink = CsvSink::open(&path).unwrap();
            sink.write(&Row {
                id: 1,
                name: "a".into(),
                price: 100,
            })
            .await
            .unwrap();
        }
        // Re-open: file exists with content → no header re-write.
        {
            let sink = CsvSink::open(&path).unwrap();
            sink.write(&Row {
                id: 2,
                name: "b".into(),
                price: 200,
            })
            .await
            .unwrap();
        }
        let content = std::fs::read_to_string(&path).unwrap();
        let header_lines: Vec<&str> = content
            .lines()
            .filter(|l| l.starts_with("id,name"))
            .collect();
        assert_eq!(header_lines.len(), 1, "header appears exactly once");
        std::fs::remove_file(&path).ok();
    }

    #[tokio::test]
    async fn data_sink_dir_writes_both_formats() {
        let dir = std::env::temp_dir().join(format!("vexerers-dir-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let sink = DataSinkDir::new(&dir);
        sink.write_many(
            "test",
            SinkFormat::Both,
            &[
                Row {
                    id: 1,
                    name: "a".into(),
                    price: 100,
                },
                Row {
                    id: 2,
                    name: "b".into(),
                    price: 200,
                },
            ],
        )
        .await
        .unwrap();
        assert!(dir.join("test.jsonl").exists());
        assert!(dir.join("test.csv").exists());
        let jsonl = std::fs::read_to_string(dir.join("test.jsonl")).unwrap();
        assert_eq!(jsonl.lines().filter(|l| !l.is_empty()).count(), 2);
        let csv = std::fs::read_to_string(dir.join("test.csv")).unwrap();
        assert_eq!(csv.lines().filter(|l| !l.is_empty()).count(), 3); // header + 2
        std::fs::remove_dir_all(&dir).ok();
    }
}
