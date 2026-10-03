//! Firecracker lifecycle with durable checkpoints and explicit crash recovery.

use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use reqwest::{Client, Method};
use serde_json::{Value, json};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};
use tokio::process::{Child, Command};
use tokio::time::{sleep, timeout};
use uuid::Uuid;

#[derive(Clone, Debug)]
pub struct VmConfig {
    pub state: PathBuf,
    pub runner: String,
    pub firecracker: String,
    pub guest_host: String,
}

pub struct Vm {
    config: VmConfig,
    meta: Value,
    child: Option<Child>,
    child_identity: Option<Value>,
    lifecycle_error: Option<String>,
    api: Client,
    api_directory: File,
    // Keep this descriptor open for the lifetime of this VM controller.
    _lock: File,
}

fn process_identity(pid: u32) -> Result<Option<Value>> {
    let stat = match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => stat,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let (_, rest) = stat
        .rsplit_once(')')
        .context("Invalid Linux process stat")?;
    let fields: Vec<_> = rest.split_whitespace().collect();
    ensure!(fields.len() > 19, "Incomplete Linux process stat");
    if matches!(fields[0], "Z" | "X") {
        return Ok(None);
    }
    Ok(Some(json!({
        "boot_id": fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim(),
        "start_ticks": fields[19],
    })))
}

fn atomic_json(path: &Path, value: &Value) -> Result<()> {
    let temporary = path.with_extension("tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&temporary)?;
    serde_json::to_writer(&mut file, value)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    File::open(path.parent().context("Metadata path needs a parent")?)?.sync_all()?;
    Ok(())
}

fn private_write(path: &Path, contents: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(contents)?;
    file.sync_all()?;
    Ok(())
}

fn checked(command: &mut std::process::Command) -> Result<()> {
    let output = command.output()?;
    ensure!(
        output.status.success(),
        "Command failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

fn initialize_credentials(state: &Path) -> Result<()> {
    let image = state.join("secrets.ext4");
    let key = state.join("id_ed25519");
    let password = state.join("vnc_password");
    if image.exists() {
        ensure!(
            key.is_file() && password.is_file(),
            "Existing secrets drive requires its original SSH key and vnc_password; restore them instead of generating replacements"
        );
        return Ok(());
    }
    if !key.exists() {
        checked(
            std::process::Command::new("ssh-keygen")
                .args(["-q", "-t", "ed25519", "-N", "", "-f"])
                .arg(&key),
        )?;
    }
    if !password.exists() {
        let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        let mut generated = Vec::new();
        while generated.len() < 8 {
            let mut random = [0_u8; 16];
            getrandom::fill(&mut random)
                .map_err(|error| anyhow::anyhow!("Secure randomness failed: {error}"))?;
            for byte in random {
                if byte < 248 && generated.len() < 8 {
                    generated.push(alphabet[(byte % 62) as usize]);
                }
            }
        }
        private_write(&password, &generated)?;
    }
    // Derive the public key from the original private key, including interrupted initialization.
    let public_key = std::process::Command::new("ssh-keygen")
        .args(["-y", "-f"])
        .arg(&key)
        .output()?;
    ensure!(
        public_key.status.success(),
        "Could not derive the existing SSH public key"
    );
    let folder = state.join(format!("credentials-{}", Uuid::new_v4().simple()));
    fs::create_dir(&folder)?;
    fs::set_permissions(&folder, fs::Permissions::from_mode(0o700))?;
    let temporary = state.join("secrets.ext4.new");
    let result = (|| -> Result<()> {
        private_write(&folder.join("authorized_keys"), &public_key.stdout)?;
        private_write(&folder.join("vnc_password"), &fs::read(&password)?)?;
        if temporary.exists() {
            fs::remove_file(&temporary)?;
        }
        checked(
            std::process::Command::new("mkfs.ext4")
                .args(["-q", "-F", "-d"])
                .arg(&folder)
                .arg(&temporary)
                .arg("4M"),
        )?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
        for instruction in [
            "set_inode_field / uid 0",
            "set_inode_field / gid 0",
            "set_inode_field / mode 040755",
            "set_inode_field /authorized_keys uid 0",
            "set_inode_field /authorized_keys mode 0100644",
        ] {
            checked(
                std::process::Command::new("debugfs")
                    .args(["-w", "-R", instruction])
                    .arg(&temporary),
            )?;
        }
        File::open(&temporary)?.sync_all()?;
        fs::rename(&temporary, &image)?;
        File::open(state)?.sync_all()?;
        Ok(())
    })();
    let cleanup = fs::remove_dir_all(&folder);
    result?;
    cleanup?;
    Ok(())
}

fn store_closure(path: &Path) -> Result<PathBuf> {
    let canonical =
        fs::canonicalize(path).with_context(|| format!("Resolve Nix path {}", path.display()))?;
    let relative = canonical
        .strip_prefix("/nix/store")
        .context("VM runtime must be in the Nix store")?;
    let first = relative
        .components()
        .next()
        .context("Missing Nix closure name")?;
    Ok(Path::new("/nix/store").join(first.as_os_str()))
}

fn milliseconds(start: Instant) -> f64 {
    (start.elapsed().as_secs_f64() * 100_000.0).round() / 100.0
}

fn firecracker_client(directory: &File) -> Result<Client> {
    Ok(Client::builder()
        .no_proxy()
        // AF_UNIX limits the socket address to 108 bytes. A directory fd keeps
        // long workspace paths private and avoids a global short-path alias.
        .unix_socket(format!(
            "/proc/self/fd/{}/firecracker.sock",
            directory.as_raw_fd()
        ))
        .timeout(Duration::from_secs(120))
        .build()?)
}

impl Vm {
    pub fn new(mut config: VmConfig) -> Result<Self> {
        fs::create_dir_all(&config.state)?;
        fs::set_permissions(&config.state, fs::Permissions::from_mode(0o700))?;
        config.state = fs::canonicalize(&config.state)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(config.state.join("controller.lock"))?;
        FileExt::try_lock_exclusive(&lock)
            .context("Another controller owns this VM state directory")?;
        initialize_credentials(&config.state)?;
        let meta_path = config.state.join("state.json");
        let meta: Value = if meta_path.exists() {
            serde_json::from_slice(&fs::read(meta_path)?)?
        } else {
            json!({"state": "new"})
        };
        ensure!(meta.is_object(), "VM metadata must be an object");
        ensure!(
            matches!(
                meta["state"].as_str(),
                Some("new" | "running" | "suspended" | "stopped")
            ),
            "Unknown VM metadata state"
        );
        let api_directory = File::open(&config.state)?;
        let api = firecracker_client(&api_directory)?;
        Ok(Self {
            config,
            meta,
            child: None,
            child_identity: None,
            lifecycle_error: None,
            api,
            api_directory,
            _lock: lock,
        })
    }

    fn save(&mut self, changes: Value) -> Result<()> {
        let mut updated = self.meta.clone();
        let fields = updated
            .as_object_mut()
            .context("Metadata must be an object")?;
        for (key, value) in changes.as_object().context("Changes must be an object")? {
            fields.insert(key.clone(), value.clone());
        }
        atomic_json(&self.config.state.join("state.json"), &updated)?;
        self.meta = updated;
        Ok(())
    }

    fn live(&mut self) -> Result<bool> {
        if let Some(child) = &mut self.child {
            if child.try_wait()?.is_none() {
                return Ok(true);
            }
        }
        self.child = None;
        self.child_identity = None;
        Ok(false)
    }

    fn orphan_pid(&mut self) -> Result<Option<u32>> {
        if self.live()? {
            return Ok(None);
        }
        let Some(pid) = self.meta["process_pid"]
            .as_u64()
            .and_then(|pid| u32::try_from(pid).ok())
        else {
            return Ok(None);
        };
        match process_identity(pid)? {
            Some(identity) if identity == self.meta["process_identity"] => Ok(Some(pid)),
            _ => Ok(None),
        }
    }

    fn require_no_orphan(&mut self) -> Result<()> {
        if let Some(pid) = self.orphan_pid()? {
            bail!(
                "Previous VM process {pid} is still alive after controller loss. Stop it before recovery; another VM could corrupt its disk"
            );
        }
        Ok(())
    }

    pub fn status(&mut self) -> Result<Value> {
        let live = self.live()?;
        let orphan = self.orphan_pid()?;
        let mut result = self.meta.clone();
        result["state"] = if live && self.meta["state"] == "suspended" {
            json!("suspending")
        } else {
            self.meta["state"].clone()
        };
        result["pid"] = json!(self.child.as_ref().and_then(Child::id));
        result["guest_host"] = json!(self.config.guest_host);
        result["guest_user"] = json!("agent");
        result["headless"] = json!(true);
        result["orphan_pid"] = json!(orphan);
        result["recovery_required"] = json!(
            self.lifecycle_error.is_some()
                || (!live && (self.meta["state"] == "running" || orphan.is_some()))
        );
        if let Some(error) = &self.lifecycle_error {
            result["lifecycle_error"] = json!(error);
        }
        Ok(result)
    }

    async fn fc(&self, method: Method, path: &str, data: Value) -> Result<()> {
        let response = self
            .api
            .request(method, format!("http://localhost{path}"))
            .json(&data)
            .send()
            .await?;
        let status = response.status();
        let body = response.text().await?;
        ensure!(status.is_success(), "Firecracker {path}: {status} {body}");
        Ok(())
    }

    async fn retain(&self, name: &str, closure: &Path) -> Result<()> {
        let root = self.config.state.join(name);
        if fs::canonicalize(&root).ok().as_deref() == Some(closure) {
            return Ok(());
        }
        let output = Command::new("nix-store")
            .args(["--add-root"])
            .arg(&root)
            .args(["--indirect", "-r"])
            .arg(closure)
            .output()
            .await?;
        ensure!(
            output.status.success(),
            "Could not retain {name} closure: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        ensure!(
            fs::canonicalize(&root)? == closure,
            "Unexpected GC root target for {name}"
        );
        Ok(())
    }

    async fn spawn(&mut self, program: &Path, arguments: &[&str]) -> Result<()> {
        let socket = self.config.state.join("firecracker.sock");
        if socket.exists() {
            fs::remove_file(&socket)?;
        }
        // Never reuse a pooled Unix-socket connection from the previous VMM.
        self.api = firecracker_client(&self.api_directory)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(self.config.state.join("console.log"))?;
        let child = Command::new(program)
            .args(arguments)
            .current_dir(&self.config.state)
            .stdin(Stdio::null())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .kill_on_drop(false)
            .spawn()?;
        let pid = child.id().context("VM process has no PID")?;
        self.child = Some(child);
        let identity = process_identity(pid)?.context("VM exited immediately; see console.log")?;
        self.child_identity = Some(identity.clone());
        if let Err(error) = self.save(json!({"process_pid": pid, "process_identity": identity})) {
            self.stop_process().await?;
            return Err(error);
        }
        for _ in 0..500 {
            ensure!(self.live()?, "VM exited; see console.log");
            if socket.exists() {
                return Ok(());
            }
            sleep(Duration::from_millis(10)).await;
        }
        bail!("Firecracker API did not start; see console.log")
    }

    async fn ready(&mut self) -> Result<()> {
        let client = Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(2))
            .build()?;
        let endpoint = format!("http://{}:8081/health", self.config.guest_host);
        timeout(Duration::from_secs(60), async {
            loop {
                ensure!(self.live()?, "VM exited before its executor became ready");
                if let Ok(response) = client.get(&endpoint).send().await {
                    if response.status().is_success() {
                        if let Ok(probe) = response.json::<Value>().await {
                            if probe["executor_ready"] == true {
                                return Ok(());
                            }
                        }
                    }
                }
                sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .context("VM readiness timed out; see console.log")?
    }

    pub async fn wake(&mut self) -> Result<()> {
        ensure!(
            self.lifecycle_error.is_none(),
            "VM lifecycle is blocked: {}",
            self.lifecycle_error.as_deref().unwrap_or("")
        );
        if self.live()? {
            ensure!(
                self.meta["state"] != "suspended",
                "Checkpoint committed but original VM is still stopping; retry suspension"
            );
            return Ok(());
        }
        self.require_no_orphan()?;
        ensure!(
            self.meta["state"] != "running",
            "Previous VM ended without a current checkpoint. Explicit recovery cold boots saved files and loses the previous live session"
        );
        let start = Instant::now();
        let api_ms;
        if self.meta["state"] == "suspended" {
            let name = self.meta["snapshot"]
                .as_str()
                .context("Suspended VM has no snapshot")?;
            ensure!(
                name.starts_with("snapshot-") && Path::new(name).components().count() == 1,
                "Invalid snapshot directory"
            );
            let snapshot = self.config.state.join(name);
            ensure!(
                snapshot.join("memory").is_file() && snapshot.join("vm.state").is_file(),
                "Checkpoint files are missing; do not cold boot automatically"
            );
            let firecracker = PathBuf::from(
                self.meta["firecracker"]
                    .as_str()
                    .context("Checkpoint has no exact Firecracker binary")?,
            );
            let runner = PathBuf::from(
                self.meta["runner"]
                    .as_str()
                    .context("Checkpoint has no exact guest runner")?,
            );
            self.retain("guest", &store_closure(&runner)?).await?;
            self.retain("firecracker", &store_closure(&firecracker)?)
                .await?;
            // Consume the RAM checkpoint before the guest can modify its disks.
            self.save(json!({"state": "running"}))?;
            self.spawn(
                &firecracker,
                &["--enable-pci", "--api-sock", "firecracker.sock"],
            )
            .await?;
            self.fc(Method::PUT, "/snapshot/load", json!({
                "snapshot_path": snapshot.join("vm.state"),
                "mem_backend": {"backend_type": "File", "backend_path": snapshot.join("memory")},
                "resume_vm": true,
            })).await?;
            api_ms = json!(milliseconds(start));
        } else {
            let runner = store_closure(Path::new(&self.config.runner))?;
            let firecracker = fs::canonicalize(&self.config.firecracker)?;
            self.retain("guest", &runner).await?;
            self.retain("firecracker", &store_closure(&firecracker)?)
                .await?;
            self.save(json!({"state": "running", "runner": runner, "firecracker": firecracker}))?;
            self.spawn(&self.config.state.join("guest/bin/microvm-run"), &[])
                .await?;
            api_ms = Value::Null;
        }
        self.ready().await?;
        self.save(json!({"last_wake_ms": milliseconds(start), "last_restore_api_ms": api_ms}))?;
        Ok(())
    }

    async fn stop_process(&mut self) -> Result<()> {
        if !self.live()? {
            return Ok(());
        }
        let child = self.child.as_mut().context("Missing VM process")?;
        let pid = child.id().context("Missing VM PID")?;
        ensure!(
            process_identity(pid)?.as_ref() == self.child_identity.as_ref(),
            "VM process identity changed; refusing to signal it"
        );
        // SIGTERM matches the old lifecycle implementation; escalate only after a durable checkpoint or guest shutdown.
        let result = unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        if result != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        match timeout(Duration::from_secs(10), child.wait()).await {
            Ok(result) => {
                result?;
            }
            Err(_) => {
                child.start_kill()?;
                timeout(Duration::from_secs(10), child.wait())
                    .await
                    .context("VM did not exit after SIGKILL")??;
            }
        }
        self.child = None;
        self.child_identity = None;
        Ok(())
    }

    fn remove_old_snapshots(&self, keep: Option<&Path>) -> Result<()> {
        for entry in fs::read_dir(&self.config.state)? {
            let entry = entry?;
            if entry.file_name().to_string_lossy().starts_with("snapshot-")
                && Some(entry.path().as_path()) != keep
            {
                ensure!(
                    entry.file_type()?.is_dir(),
                    "Unexpected snapshot entry type"
                );
                fs::remove_dir_all(entry.path())?;
            }
        }
        File::open(&self.config.state)?.sync_all()?;
        Ok(())
    }

    pub async fn suspend(&mut self) -> Result<()> {
        ensure!(
            self.lifecycle_error.is_none(),
            "VM lifecycle is blocked: {}",
            self.lifecycle_error.as_deref().unwrap_or("")
        );
        if !self.live()? {
            self.require_no_orphan()?;
            ensure!(
                self.meta["state"] != "running",
                "VM exited without a current checkpoint; explicit recovery is required"
            );
            return Ok(());
        }
        if self.meta["state"] == "suspended" {
            // A prior attempt committed successfully, but termination did not finish.
            self.stop_process().await?;
            return Ok(());
        }
        let start = Instant::now();
        let snapshot = self
            .config
            .state
            .join(format!("snapshot-{}", Uuid::new_v4().simple()));
        fs::create_dir(&snapshot)?;
        fs::set_permissions(&snapshot, fs::Permissions::from_mode(0o700))?;
        let mut paused = false;
        let mut commit_attempted = false;
        let result = async {
            // A disconnected pause response has an unknown result. Attempt a safe resume on failure.
            paused = true;
            self.fc(Method::PATCH, "/vm", json!({"state": "Paused"}))
                .await?;
            self.fc(
                Method::PUT,
                "/snapshot/create",
                json!({
                    "snapshot_type": "Full", "snapshot_path": snapshot.join("vm.state"),
                    "mem_file_path": snapshot.join("memory"),
                }),
            )
            .await?;
            for path in [
                snapshot.join("memory"),
                snapshot.join("vm.state"),
                self.config.state.join("state.ext4"),
            ] {
                File::open(path)?.sync_all()?;
            }
            File::open(&snapshot)?.sync_all()?;
            commit_attempted = true;
            self.save(json!({"state": "suspended", "snapshot": snapshot.file_name().and_then(|name| name.to_str()).context("Invalid snapshot name")?}))?;
            Ok::<_, anyhow::Error>(())
        }
        .await;
        if let Err(error) = result {
            if commit_attempted {
                // The rename might have succeeded before its directory fsync failed.
                // Keep CPUs paused: resuming could invalidate an on-disk checkpoint.
                self.lifecycle_error = Some(format!(
                    "Checkpoint manifest durability is uncertain; VM remains paused: {error}"
                ));
                return Err(error);
            }
            if paused && self.live()? {
                // Retain incomplete artifacts if resuming also fails, for explicit inspection.
                if let Err(resume_error) = self
                    .fc(Method::PATCH, "/vm", json!({"state": "Resumed"}))
                    .await
                {
                    self.lifecycle_error = Some(format!(
                        "Snapshot failed and guest resume failed: {resume_error}"
                    ));
                    return Err(
                        resume_error.context("Snapshot failed and the guest could not resume")
                    );
                }
            }
            fs::remove_dir_all(&snapshot)?;
            return Err(error);
        }
        self.stop_process().await?;
        self.remove_old_snapshots(Some(&snapshot))?;
        self.save(json!({"last_suspend_ms": milliseconds(start)}))?;
        Ok(())
    }

    pub async fn shutdown(&mut self) -> Result<()> {
        self.wake().await?;
        let log_path = self.config.state.join("console.log");
        let offset = fs::metadata(&log_path)?.len();
        let output = timeout(
            Duration::from_secs(30),
            Command::new("ssh")
                .arg("-i")
                .arg(self.config.state.join("id_ed25519"))
                .args([
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                ])
                .arg(format!(
                    "UserKnownHostsFile={}",
                    self.config.state.join("known_hosts").display()
                ))
                .arg(format!("root@{}", self.config.guest_host))
                .arg("sync; systemctl poweroff")
                .kill_on_drop(true)
                .output(),
        )
        .await
        .context("Guest shutdown SSH timed out")??;
        ensure!(
            output.status.success(),
            "Guest shutdown failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let mut halted = false;
        for _ in 0..300 {
            if !self.live()? {
                halted = true;
                break;
            }
            let mut log = File::open(&log_path)?;
            log.seek(SeekFrom::Start(offset))?;
            let mut current = Vec::new();
            log.read_to_end(&mut current)?;
            if current
                .windows(b"System halted".len())
                .any(|window| window == b"System halted")
                || current
                    .windows(b"reboot: Power down".len())
                    .any(|window| window == b"reboot: Power down")
            {
                halted = true;
                break;
            }
            sleep(Duration::from_millis(100)).await;
        }
        ensure!(halted, "Guest did not finish shutting down; VM left intact");
        self.stop_process().await?;
        self.save(json!({"state": "stopped", "snapshot": null}))?;
        self.remove_old_snapshots(None)?;
        Ok(())
    }

    pub async fn recover(&mut self) -> Result<()> {
        ensure!(
            self.lifecycle_error.is_none(),
            "A lifecycle failure requires inspection before recovery"
        );
        ensure!(
            !self.live()? && self.meta["state"] == "running",
            "Recovery applies only to an unexpectedly terminated VM"
        );
        self.require_no_orphan()?;
        self.save(json!({"state": "new"}))?;
        self.wake().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fixture(meta: Value) -> Result<(TempDir, Vm)> {
        let directory = tempfile::tempdir()?;
        for name in ["secrets.ext4", "id_ed25519", "vnc_password"] {
            private_write(&directory.path().join(name), b"fixture")?;
        }
        atomic_json(&directory.path().join("state.json"), &meta)?;
        let vm = Vm::new(VmConfig {
            state: directory.path().to_path_buf(),
            runner: "/not-used".into(),
            firecracker: "/not-used".into(),
            guest_host: "127.0.0.1".into(),
        })?;
        Ok((directory, vm))
    }

    #[test]
    fn lock_prevents_two_controllers() -> Result<()> {
        let (_directory, vm) = fixture(json!({"state": "new"}))?;
        assert!(Vm::new(vm.config.clone()).is_err());
        Ok(())
    }

    #[test]
    fn orphan_detection_checks_boot_and_process_start() -> Result<()> {
        let pid = std::process::id();
        let identity = process_identity(pid)?.unwrap();
        let (_directory, mut vm) =
            fixture(json!({"state": "running", "process_pid": pid, "process_identity": identity}))?;
        assert_eq!(vm.orphan_pid()?, Some(pid));
        assert!(vm.status()?["recovery_required"].as_bool().unwrap());
        vm.meta["process_identity"]["start_ticks"] = json!("different-process");
        assert_eq!(vm.orphan_pid()?, None);
        Ok(())
    }

    #[tokio::test]
    async fn crashed_vm_never_automatically_restores_old_ram() -> Result<()> {
        let (_directory, mut vm) =
            fixture(json!({"state": "running", "snapshot": "snapshot-old"}))?;
        let error = vm.wake().await.unwrap_err();
        assert!(error.to_string().contains("Explicit recovery"));
        assert_eq!(vm.meta["state"], "running");
        Ok(())
    }

    #[tokio::test]
    async fn explicit_recovery_refuses_live_orphan() -> Result<()> {
        let pid = std::process::id();
        let (_directory, mut vm) = fixture(
            json!({"state": "running", "process_pid": pid, "process_identity": process_identity(pid)?}),
        )?;
        assert!(
            vm.recover()
                .await
                .unwrap_err()
                .to_string()
                .contains("still alive")
        );
        assert_eq!(vm.meta["state"], "running");
        Ok(())
    }

    #[tokio::test]
    async fn uncertain_manifest_blocks_resume_even_when_child_is_gone() -> Result<()> {
        let (_directory, mut vm) =
            fixture(json!({"state": "suspended", "snapshot": "snapshot-old"}))?;
        vm.lifecycle_error = Some("Manifest fsync failed".into());
        assert!(vm.status()?["recovery_required"].as_bool().unwrap());
        assert!(vm.wake().await.unwrap_err().to_string().contains("blocked"));
        assert!(vm.suspend().await.is_err());
        assert!(vm.recover().await.is_err());
        Ok(())
    }

    #[tokio::test]
    async fn invalid_snapshot_path_does_not_consume_checkpoint() -> Result<()> {
        let (_directory, mut vm) =
            fixture(json!({"state": "suspended", "snapshot": "snapshot-../elsewhere"}))?;
        assert!(
            vm.wake()
                .await
                .unwrap_err()
                .to_string()
                .contains("Invalid snapshot")
        );
        assert_eq!(vm.meta["state"], "suspended");
        Ok(())
    }

    #[test]
    fn runtime_roots_cannot_point_outside_nix_store() -> Result<()> {
        let directory = tempfile::tempdir()?;
        assert!(store_closure(directory.path()).is_err());
        Ok(())
    }

    #[test]
    fn credentials_never_replace_existing_drive_without_original_keys() -> Result<()> {
        let directory = tempfile::tempdir()?;
        private_write(&directory.path().join("secrets.ext4"), b"original")?;
        assert!(initialize_credentials(directory.path()).is_err());
        assert_eq!(
            fs::read(directory.path().join("secrets.ext4"))?,
            b"original"
        );
        assert!(!directory.path().join("id_ed25519").exists());
        Ok(())
    }

    #[test]
    fn atomic_metadata_retains_python_schema_and_private_permissions() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let path = directory.path().join("state.json");
        let metadata = json!({"state": "suspended", "snapshot": "snapshot-test", "process_identity": {"boot_id": "boot", "start_ticks": "123"}});
        atomic_json(&path, &metadata)?;
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&path)?)?,
            metadata
        );
        assert_eq!(fs::metadata(path)?.permissions().mode() & 0o777, 0o600);
        assert!(!directory.path().join("state.tmp").exists());
        Ok(())
    }
}

#[cfg(test)]
mod long_socket_path_tests {
    use super::*;
    #[tokio::test]
    async fn api_connects_when_workspace_socket_path_exceeds_unix_limit() {
        use std::os::unix::net::UnixListener;
        let temporary = tempfile::tempdir().unwrap();
        let state = temporary
            .path()
            .join("a".repeat(80))
            .join("pc-400622289f8d42c8a3662a401bbb3bcf");
        fs::create_dir_all(&state).unwrap();
        assert!(state.join("firecracker.sock").as_os_str().len() > 108);
        let directory = File::open(&state).unwrap();
        let address = format!("/proc/self/fd/{}/firecracker.sock", directory.as_raw_fd());
        let listener = UnixListener::bind(address).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 2048];
            stream.read(&mut request).unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                .unwrap();
        });
        let response = firecracker_client(&directory)
            .unwrap()
            .get("http://localhost/vm")
            .send()
            .await
            .unwrap();
        assert!(response.status().is_success());
        assert_eq!(response.text().await.unwrap(), "{}");
        server.join().unwrap();
    }
}
