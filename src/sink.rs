//! Append-only JSONL sink with crash-safe per-line flushes.
//!
//! Each entity type (trips, areas, schedules, …) gets its own `.jsonl` file
//! under a chosen directory. Writes are line-buffered and flushed after every
//! line so crawler progress survives a crash mid-run.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;
use tokio::sync::Mutex;

use crate::error::Result;

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

/// A directory of JSONL sinks, one per entity type. Sinks are lazily opened
/// on first write so unused entity files aren't created.
pub struct JsonlSinkDir {
    root: PathBuf,
    inner: Arc<Mutex<HashMap<String, Arc<JsonlSink>>>>,
}

impl JsonlSinkDir {
    /// Create a sink directory rooted at `root`. The directory itself is
    /// created lazily on first write.
    #[must_use]
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self {
            root: root.as_ref().to_path_buf(),
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Append one record to `<root>/<name>.jsonl`.
    pub async fn write<T: Serialize + Send>(&self, name: &str, record: &T) -> Result<()> {
        let sink = self.get_or_open(name).await?;
        sink.write(record).await
    }

    /// Append many records at once to `<root>/<name>.jsonl`.
    pub async fn write_many<T: Serialize + Send>(
        &self,
        name: &str,
        records: &[T],
    ) -> Result<usize> {
        let sink = self.get_or_open(name).await?;
        sink.write_many(records).await
    }

    async fn get_or_open(&self, name: &str) -> Result<Arc<JsonlSink>> {
        let mut map = self.inner.lock().await;
        if let Some(s) = map.get(name) {
            return Ok(Arc::clone(s));
        }
        let path = self.root.join(format!("{name}.jsonl"));
        let sink = Arc::new(JsonlSink::open(path)?);
        map.insert(name.to_string(), Arc::clone(&sink));
        Ok(sink)
    }
}
