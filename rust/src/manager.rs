use crate::vm::{Vm, VmConfig};
use anyhow::{Context, Result, bail};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use rusqlite::{Connection, params};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex as AsyncMutex;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Deserialize)]
pub struct Slot {
    pub runner: String,
    pub firecracker: String,
    pub guest_host: String,
}

pub struct Settings {
    pub state: PathBuf,
    pub slots: Vec<Slot>,
    pub port: u16,
    pub idle: Duration,
    pub reserve_mb: u64,
}

#[derive(Clone)]
pub struct Binding {
    pub id: String,
    pub slot: usize,
    pub guest_host: String,
    pub runner: String,
    pub token: String,
}

struct Workspace {
    binding: Binding,
    vm: AsyncMutex<Vm>,
    lifecycle: AsyncMutex<()>,
    busy: AtomicUsize,
    queued: AtomicUsize,
    writer: AtomicBool,
    last_activity: Mutex<Instant>,
}

pub struct Manager {
    settings: Settings,
    db: Mutex<Connection>,
    workspaces: RwLock<BTreeMap<String, Arc<Workspace>>>,
    snapshot: AsyncMutex<()>,
    admission: AsyncMutex<()>,
    pub shutdown_token: CancellationToken,
}

pub struct Lease {
    workspace: Arc<Workspace>,
}
impl Drop for Lease {
    fn drop(&mut self) {
        *self.workspace.last_activity.lock().unwrap() = Instant::now();
        self.workspace.busy.fetch_sub(1, Ordering::SeqCst);
    }
}

pub struct WriterGuard {
    workspace: Arc<Workspace>,
}
impl Drop for WriterGuard {
    fn drop(&mut self) {
        self.workspace.writer.store(false, Ordering::SeqCst);
    }
}

struct QueueGuard {
    workspace: Arc<Workspace>,
}
impl Drop for QueueGuard {
    fn drop(&mut self) {
        self.workspace.queued.fetch_sub(1, Ordering::SeqCst);
    }
}

fn timestamp() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs_f64()
}

pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 48
        && id.as_bytes()[0].is_ascii_alphanumeric()
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

impl Manager {
    pub fn new(settings: Settings) -> Result<Arc<Self>> {
        let db = Connection::open(settings.state.join("state.sqlite"))?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "synchronous", "FULL")?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, slot INTEGER UNIQUE, token TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, workspace TEXT, method TEXT, state TEXT, created REAL, finished REAL);
            CREATE INDEX IF NOT EXISTS operations_created ON operations(created DESC);")?;
        db.execute(
            "UPDATE operations SET state='unknown', finished=? WHERE state='pending'",
            [timestamp()],
        )?;
        let bindings = {
            let mut stmt = db.prepare("SELECT id,slot,token FROM workspaces ORDER BY slot")?;
            stmt.query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?
        };
        let manager = Arc::new(Self {
            settings,
            db: Mutex::new(db),
            workspaces: RwLock::new(BTreeMap::new()),
            snapshot: AsyncMutex::new(()),
            admission: AsyncMutex::new(()),
            shutdown_token: CancellationToken::new(),
        });
        for (id, slot, token) in bindings {
            if !valid_id(&id) {
                bail!("Stored workspace ID is invalid");
            }
            let workspace = manager.make_workspace(
                id.clone(),
                usize::try_from(slot).context("Invalid stored workspace slot")?,
                token,
            )?;
            manager.workspaces.write().unwrap().insert(id, workspace);
        }
        Ok(manager)
    }

    fn make_workspace(&self, id: String, slot: usize, token: String) -> Result<Arc<Workspace>> {
        let config = self
            .settings
            .slots
            .get(slot)
            .context("Stored workspace slot is not configured")?;
        let binding = Binding {
            id: id.clone(),
            slot,
            runner: config.runner.clone(),
            guest_host: config.guest_host.clone(),
            token,
        };
        let vm = Vm::new(VmConfig {
            state: self.settings.state.join("workspaces").join(id),
            runner: config.runner.clone(),
            firecracker: config.firecracker.clone(),
            guest_host: config.guest_host.clone(),
        })?;
        Ok(Arc::new(Workspace {
            binding,
            vm: AsyncMutex::new(vm),
            lifecycle: AsyncMutex::new(()),
            busy: AtomicUsize::new(0),
            queued: AtomicUsize::new(0),
            writer: AtomicBool::new(false),
            last_activity: Mutex::new(Instant::now()),
        }))
    }

    fn workspace(&self, id: &str) -> Result<Arc<Workspace>> {
        self.workspaces
            .read()
            .unwrap()
            .get(id)
            .cloned()
            .context("Workspace does not exist")
    }

    pub fn binding(&self, id: &str) -> Result<Binding> {
        Ok(self.workspace(id)?.binding.clone())
    }

    pub fn allocate(&self, id: &str) -> Result<Value> {
        if !valid_id(id) {
            bail!("Workspace ID must contain 1 to 48 letters, digits, underscores, or hyphens");
        }
        if self.shutdown_token.is_cancelled() {
            bail!("Orchestrator is stopping");
        }
        let mut workspaces = self.workspaces.write().unwrap();
        if let Some(workspace) = workspaces.get(id) {
            return Ok(self.public(&workspace.binding, true));
        }
        let slot = (0..self.settings.slots.len())
            .find(|slot| !workspaces.values().any(|w| w.binding.slot == *slot))
            .context("All configured workspace slots are allocated. No workspace was deleted.")?;
        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes)
            .map_err(|e| anyhow::anyhow!("Capability generation failed: {e}"))?;
        let token = URL_SAFE_NO_PAD.encode(bytes);
        let workspace = self.make_workspace(id.to_owned(), slot, token.clone())?;
        self.db.lock().unwrap().execute(
            "INSERT INTO workspaces VALUES(?,?,?,?)",
            params![id, i64::try_from(slot)?, token, timestamp()],
        )?;
        let result = self.public(&workspace.binding, true);
        workspaces.insert(id.to_owned(), workspace);
        Ok(result)
    }

    fn public(&self, binding: &Binding, token: bool) -> Value {
        let mut value = json!({"id":binding.id,"slot":binding.slot,"guest_host":binding.guest_host,"runner":binding.runner,"generation":binding.runner,"workspace_path":"/var/lib/agent/workspace","exec_url":format!("ws://127.0.0.1:{}/workspaces/{}/exec",self.settings.port,binding.id)});
        if token {
            value["auth_bearer_token"] = json!(binding.token);
        }
        value
    }

    pub async fn status(&self, id: &str) -> Result<Value> {
        let workspace = self.workspace(id)?;
        let mut result = self.public(&workspace.binding, false);
        let status = workspace.vm.lock().await.status()?;
        result.as_object_mut().unwrap().extend(
            status
                .as_object()
                .context("VM status is not an object")?
                .clone(),
        );
        result["generation"] = result
            .get("runner")
            .cloned()
            .unwrap_or_else(|| json!(workspace.binding.runner));
        result["active_operations"] = json!(workspace.busy.load(Ordering::SeqCst));
        result["leases"] = result["active_operations"].clone();
        result["queued_operations"] = json!(workspace.queued.load(Ordering::SeqCst));
        result["writer_connected"] = json!(workspace.writer.load(Ordering::SeqCst));
        result["idle_seconds"] = json!(self.settings.idle.as_secs_f64());
        Ok(result)
    }

    pub async fn list(&self) -> Result<Value> {
        let mut ids = self
            .workspaces
            .read()
            .unwrap()
            .values()
            .map(|w| (w.binding.slot, w.binding.id.clone()))
            .collect::<Vec<_>>();
        ids.sort();
        let mut result = Vec::new();
        for (_, id) in ids {
            result.push(self.status(&id).await?);
        }
        Ok(
            json!({"workspaces":result,"slots":self.settings.slots.len(),"host_reserve_mb":self.settings.reserve_mb}),
        )
    }

    pub fn claim_writer(self: &Arc<Self>, id: &str) -> Result<WriterGuard> {
        if self.shutdown_token.is_cancelled() {
            bail!("Orchestrator is stopping");
        }
        let workspace = self.workspace(id)?;
        workspace
            .writer
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| anyhow::anyhow!("This workspace already has an attached harness"))?;
        Ok(WriterGuard { workspace })
    }

    /// Observe host metadata without leases, guest probes, or waiting for VM mutations.
    pub fn observation(&self) -> Value {
        use std::os::unix::fs::MetadataExt;
        let mut workspaces = self
            .workspaces
            .read()
            .unwrap()
            .values()
            .cloned()
            .collect::<Vec<_>>();
        workspaces.sort_by_key(|workspace| workspace.binding.slot);
        let rows = workspaces
            .iter()
            .map(|workspace| {
                let mut row = self.public(&workspace.binding, false);
                match workspace.vm.try_lock() {
                    Ok(mut vm) => match vm.status() {
                        Ok(status) => row
                            .as_object_mut()
                            .unwrap()
                            .extend(status.as_object().unwrap().clone()),
                        Err(error) => {
                            row["state"] = json!("error");
                            row["error"] = json!(error.to_string());
                        }
                    },
                    Err(_) => {
                        row["state"] = json!("transitioning");
                    }
                }
                row["active_operations"] = json!(workspace.busy.load(Ordering::SeqCst));
                row["queued_operations"] = json!(workspace.queued.load(Ordering::SeqCst));
                row["writer_connected"] = json!(workspace.writer.load(Ordering::SeqCst));
                row["idle_elapsed_seconds"] = json!(
                    workspace
                        .last_activity
                        .lock()
                        .unwrap()
                        .elapsed()
                        .as_secs_f64()
                );
                row["memory_bound_bytes"] = json!(1024_u64 * 1024 * 1024);
                let folder = self
                    .settings
                    .state
                    .join("workspaces")
                    .join(&workspace.binding.id);
                if let Ok(disk) = std::fs::metadata(folder.join("state.ext4")) {
                    row["disk_bytes"] = json!(disk.len());
                    row["disk_allocated_bytes"] = json!(disk.blocks() * 512);
                }
                if let Some(name) = row["snapshot"].as_str().filter(|name| {
                    name.starts_with("snapshot-")
                        && std::path::Path::new(name).components().count() == 1
                }) {
                    let allocated: u64 = ["memory", "vm.state"]
                        .into_iter()
                        .filter_map(|file| std::fs::metadata(folder.join(name).join(file)).ok())
                        .map(|file| file.blocks() * 512)
                        .sum();
                    row["checkpoint_bytes"] = json!(allocated);
                }
                if let Some(pid) = row["pid"].as_u64() {
                    if let Ok(status) = std::fs::read_to_string(format!("/proc/{pid}/status")) {
                        if let Some(rss) = status
                            .lines()
                            .find(|line| line.starts_with("VmRSS:"))
                            .and_then(|line| line.split_whitespace().nth(1))
                            .and_then(|value| value.parse::<u64>().ok())
                        {
                            row["rss_bytes"] = json!(rss * 1024);
                        }
                    }
                }
                row
            })
            .collect::<Vec<_>>();
        let recent = self.recent_operations().unwrap_or_else(|error| {
            vec![json!({"state":"error","method":"journal","error":error.to_string()})]
        });
        json!({"workspaces":rows,"slots":self.settings.slots.len(),"host_reserve_bytes":self.settings.reserve_mb * 1024 * 1024,"idle_seconds":self.settings.idle.as_secs_f64(),"recent_operations":recent})
    }

    fn recent_operations(&self) -> Result<Vec<Value>> {
        let db = self.db.lock().unwrap();
        let mut statement = db.prepare_cached("SELECT workspace,method,state,created,finished FROM operations ORDER BY created DESC LIMIT 20")?;
        Ok(statement.query_map([], |row| Ok(json!({
            "workspace":row.get::<_, Option<String>>(0)?, "method":row.get::<_, Option<String>>(1)?,
            "state":row.get::<_, Option<String>>(2)?, "created":row.get::<_, Option<f64>>(3)?,
            "finished":row.get::<_, Option<f64>>(4)?,
        })))?.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    fn check_cancel(&self, cancel: &CancellationToken) -> Result<()> {
        if self.shutdown_token.is_cancelled() {
            bail!("Orchestrator is stopping");
        }
        if cancel.is_cancelled() {
            bail!("Request disconnected before execution");
        }
        Ok(())
    }

    pub async fn acquire(self: &Arc<Self>, id: &str, cancel: &CancellationToken) -> Result<Lease> {
        let workspace = self.workspace(id)?;
        let _lifecycle = tokio::select! {
            guard = workspace.lifecycle.lock() => guard,
            _ = cancel.cancelled() => bail!("Request disconnected before execution"),
            _ = self.shutdown_token.cancelled() => bail!("Orchestrator is stopping"),
        };
        self.check_cancel(cancel)?;
        let status = workspace.vm.lock().await.status()?;
        if status["recovery_required"] == true || status["state"] == "suspending" {
            bail!("VM lifecycle requires inspection or explicit recovery before execution");
        }
        let running = status.get("pid").is_some_and(|pid| !pid.is_null());
        if !running {
            let _admission = self.wait_admission(&workspace, cancel).await?;
            // Complete lifecycle mutations even if the client leaves during startup.
            workspace.vm.lock().await.wake().await?;
        } else {
            workspace.vm.lock().await.wake().await?;
        }
        self.check_cancel(cancel)?;
        workspace.busy.fetch_add(1, Ordering::SeqCst);
        let lease = Lease {
            workspace: workspace.clone(),
        };
        let folder = self.settings.state.join("workspaces").join(id);
        if !folder.join("known_hosts").exists() {
            let output = tokio::process::Command::new("ssh")
                .args([
                    "-i",
                    folder
                        .join("id_ed25519")
                        .to_str()
                        .context("Invalid key path")?,
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    &format!(
                        "UserKnownHostsFile={}",
                        folder.join("known_hosts").display()
                    ),
                    &format!("root@{}", workspace.binding.guest_host),
                    "true",
                ])
                .kill_on_drop(true)
                .output();
            let output = tokio::time::timeout(Duration::from_secs(10), output)
                .await
                .context("Guest shutdown identity probe timed out")??;
            if !output.status.success() {
                bail!(
                    "Could not establish guest shutdown identity: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
            }
        }
        self.check_cancel(cancel)?;
        Ok(lease)
    }

    async fn wait_admission<'a>(
        &'a self,
        workspace: &Arc<Workspace>,
        cancel: &CancellationToken,
    ) -> Result<tokio::sync::MutexGuard<'a, ()>> {
        workspace.queued.fetch_add(1, Ordering::SeqCst);
        let _queue = QueueGuard {
            workspace: workspace.clone(),
        };
        let deadline = Instant::now() + Duration::from_secs(120);
        loop {
            self.check_cancel(cancel)?;
            let admission = tokio::select! {
                guard = self.admission.lock() => guard,
                _ = cancel.cancelled() => bail!("Request disconnected before execution"),
                _ = self.shutdown_token.cancelled() => bail!("Orchestrator is stopping"),
            };
            self.check_cancel(cancel)?;
            let others = self
                .workspaces
                .read()
                .unwrap()
                .values()
                .filter(|other| other.binding.id != workspace.binding.id)
                .cloned()
                .collect::<Vec<_>>();
            let mut active = 0;
            for other in others {
                if other
                    .vm
                    .lock()
                    .await
                    .status()?
                    .get("pid")
                    .is_some_and(|pid| !pid.is_null())
                {
                    active += 1;
                }
            }
            if available_memory_mb()?
                >= self.settings.reserve_mb.saturating_add(1024 * (active + 1))
            {
                self.check_cancel(cancel)?;
                return Ok(admission);
            }
            drop(admission);
            if Instant::now() >= deadline {
                bail!("Admission timed out. Host memory reserve prevented guest startup.");
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(500)) => {},
                _ = cancel.cancelled() => bail!("Request disconnected before execution"),
                _ = self.shutdown_token.cancelled() => bail!("Orchestrator is stopping"),
            }
        }
    }

    pub async fn suspend(&self, id: &str) -> Result<()> {
        let workspace = self.workspace(id)?;
        let _snapshot = self.snapshot.lock().await;
        let _lifecycle = workspace.lifecycle.lock().await;
        if workspace.busy.load(Ordering::SeqCst) != 0 {
            bail!("Workspace has active operations or processes");
        }
        workspace.vm.lock().await.suspend().await
    }

    pub async fn admin_action(self: &Arc<Self>, id: &str, action: &str) -> Result<Value> {
        if self.shutdown_token.is_cancelled() {
            bail!("Orchestrator is stopping");
        }
        match action {
            "resume" => {
                drop(self.acquire(id, &self.shutdown_token).await?);
            }
            "suspend" => {
                self.suspend(id).await?;
            }
            "shutdown" | "recover" => {
                let workspace = self.workspace(id)?;
                let _lifecycle = workspace.lifecycle.lock().await;
                if workspace.busy.load(Ordering::SeqCst) != 0
                    || workspace.writer.load(Ordering::SeqCst)
                {
                    bail!("Workspace has an active writer or operation");
                }
                let running = workspace
                    .vm
                    .lock()
                    .await
                    .status()?
                    .get("pid")
                    .is_some_and(|pid| !pid.is_null());
                let _admission = if !running {
                    Some(
                        self.wait_admission(&workspace, &self.shutdown_token)
                            .await?,
                    )
                } else {
                    None
                };
                let mut vm = workspace.vm.lock().await;
                if action == "shutdown" {
                    vm.shutdown().await?;
                } else {
                    vm.recover().await?;
                }
                *workspace.last_activity.lock().unwrap() = Instant::now();
            }
            _ => bail!("Unknown action"),
        }
        Ok(json!({"ok":true}))
    }

    pub fn operation_begin(&self, key: &str, workspace: &str, method: &str) -> Result<()> {
        self.db.lock().unwrap().execute(
            "INSERT INTO operations VALUES(?,?,?,?,?,NULL)",
            params![key, workspace, method, "pending", timestamp()],
        )?;
        Ok(())
    }

    pub fn operation_finish(&self, key: &str, outcome: &str) -> Result<()> {
        self.db.lock().unwrap().execute(
            "UPDATE operations SET state=?,finished=? WHERE id=?",
            params![outcome, timestamp(), key],
        )?;
        Ok(())
    }

    pub async fn idle_loop(self: Arc<Self>) {
        let interval =
            (self.settings.idle / 4).clamp(Duration::from_millis(100), Duration::from_secs(5));
        loop {
            tokio::select! { _ = self.shutdown_token.cancelled() => break, _ = tokio::time::sleep(interval) => {} }
            if self.settings.idle.is_zero() {
                continue;
            }
            let workspaces = self
                .workspaces
                .read()
                .unwrap()
                .values()
                .cloned()
                .collect::<Vec<_>>();
            for workspace in workspaces {
                if workspace.busy.load(Ordering::SeqCst) != 0
                    || workspace.last_activity.lock().unwrap().elapsed() < self.settings.idle
                {
                    continue;
                }
                // Queued startup must not block idle suspension of another VM.
                let Ok(_snapshot) = self.snapshot.try_lock() else {
                    continue;
                };
                let Ok(_lifecycle) = workspace.lifecycle.try_lock() else {
                    continue;
                };
                if workspace.busy.load(Ordering::SeqCst) != 0
                    || workspace.last_activity.lock().unwrap().elapsed() < self.settings.idle
                {
                    continue;
                }
                if let Err(error) = workspace.vm.lock().await.suspend().await {
                    tracing::warn!(workspace=%workspace.binding.id,%error,"Could not suspend workspace");
                }
            }
        }
    }

    pub async fn stop(&self) -> Result<()> {
        self.shutdown_token.cancel();
        let deadline = Instant::now() + Duration::from_secs(50);
        loop {
            let busy = self
                .workspaces
                .read()
                .unwrap()
                .values()
                .any(|w| w.writer.load(Ordering::SeqCst) || w.busy.load(Ordering::SeqCst) != 0);
            if !busy {
                break;
            }
            if Instant::now() >= deadline {
                bail!(
                    "Execution connections did not drain. Refusing to snapshot active operations"
                );
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let ids = self
            .workspaces
            .read()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        let mut errors = Vec::new();
        for id in ids {
            if let Err(error) = self.suspend(&id).await {
                errors.push(format!("Could not suspend {id}: {error:#}"));
            }
        }
        if !errors.is_empty() {
            bail!("{}", errors.join(". "));
        }
        Ok(())
    }
}

fn available_memory_mb() -> Result<u64> {
    let info = std::fs::read_to_string("/proc/meminfo")?;
    let line = info
        .lines()
        .find(|line| line.starts_with("MemAvailable:"))
        .context("MemAvailable is missing")?;
    Ok(line
        .split_whitespace()
        .nth(1)
        .context("Invalid MemAvailable")?
        .parse::<u64>()?
        / 1024)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn workspace_ids_cannot_escape_state_directory() {
        for id in ["a", "swe-demo_a", "Z9", &"a".repeat(48)] {
            assert!(valid_id(id));
        }
        for id in [
            "",
            "../escape",
            "/a",
            "a/b",
            " a",
            "é",
            "-a",
            &"a".repeat(49),
        ] {
            assert!(!valid_id(id));
        }
    }
    #[test]
    fn host_memory_is_available() {
        assert!(available_memory_mb().unwrap() > 0);
    }
}
