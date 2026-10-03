# Environment orchestrator

Run agent harnesses on the host. Give each harness an isolated Firecracker workspace for commands and files. Restore a suspended workspace when its agent needs a tool. Suspend the workspace after its work stops.

The service and VM controller use Rust. The CLI and Codex launcher use short-lived Python processes. Nix builds the service and its dependencies. NixOS builds the guest images.

## Architecture

```mermaid
flowchart LR
    H[Host Codex harness] -->|Workspace capability| P[Execution proxy]
    subgraph R[One Rust orchestrator process]
        P --> M[Workspace manager and admission]
        M --> V[Firecracker lifecycle]
        M --> D[(Private SQLite journal)]
    end
    V --> G[Guest executor]
    G --> F[Persistent workspace disk]
    V --> S[RAM checkpoint and exact Nix closures]
    H --> I[OpenAI inference service]
    A[Administrative CLI] -->|Private Unix socket| M
```

The administrative API uses a private Unix socket. The execution gateway listens on loopback port 6090. Each workspace has a separate capability, disk, SSH key, executor session, and conversation directory. One harness can own a workspace at a time.

The first deployment provides two fixed workspace slots. Each guest has two vCPUs, 1 GiB RAM, and an 8 GiB sparse persistent disk. The service can accept a different slot list. Automatic slot creation and recycling are later work.

The Codex launcher uses remote execution for shell, processes, files, patches, images, and project instructions. Bubblewrap hides host workspace files and credentials from the harness. It exposes immutable Nix tools, shared skills, and private conversation state. See [Codex routing](CODEX.md).

## Build and use

```sh
nix build
./result/bin/environment-vm status
./result/bin/environment-vm create swe-demo-a
./result/bin/environment-codex swe-demo-a
./result/bin/environment-vm suspend swe-demo-a
./result/bin/environment-vm resume swe-demo-a
```

These commands require a running service and configured guest slots. Creating a workspace and reading its status do not start its VM.

Machine-specific NixOS guest and network modules live in the private `mbp-agent-nixos` repository. That repository pins this source by commit. Import `package.nix` with `pkgs` and, optionally, a separate `codex` package.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT_STATE` | `~/.local/share/environment-orchestrator` | Private database, disks, checkpoints, credentials, and conversations |
| `ENVIRONMENT_SLOTS` | `[]` | JSON array with `runner`, `firecracker`, and `guest_host` per slot |
| `ENVIRONMENT_PORT` | `6090` | Loopback execution gateway port |
| `ENVIRONMENT_IDLE_SECONDS` | `120` | Time without an execution lease before suspension |
| `ENVIRONMENT_HOST_RESERVE_MB` | `3072` | Host memory reserve before starting a 1 GiB guest |
| `RUST_LOG` | `environment_orchestrator=info` | Service log filter |

The slot runner and Firecracker binary must exist in the Nix store. The host must provide KVM, writable TAP devices, guest routing, and access to the Nix daemon. The service wrapper supplies Nix-managed SSH and filesystem tools.

## Suspension and recovery

```mermaid
stateDiagram-v2
    [*] --> New
    New --> Running: Admit memory and cold boot
    Running --> Suspended: No leases, pause, snapshot, commit, terminate
    Suspended --> Running: Consume checkpoint and restore exact runtime
    Running --> RecoveryRequired: Unexpected VM exit
    RecoveryRequired --> Running: Explicit recovery with saved files
    Running --> Stopped: Graceful guest shutdown
    Stopped --> Running: Cold boot configured image
```

Pending requests and running executor commands retain execution leases. An idle WebSocket does not retain a lease. The proxy answers initialized health probes without waking the guest. It retains the guest TCP connection across suspension.

The service pauses the guest and writes a full checkpoint. It synchronizes checkpoint files and the persistent disk before it commits the metadata. It terminates the VMM after that commit. Restore consumes the checkpoint before the guest can change its disk.

A checkpoint retains its exact guest and Firecracker closures through Nix GC roots. A service update does not replace the suspended guest image. Never change a suspended guest disk to apply an update.

Memory admission reserves host capacity before starting a guest. Other active guests increase the reserve. Requests can wait for up to 120 seconds. A client disconnect cancels queued work before startup.

Manual suspension rejects active requests and processes. Service shutdown cancels execution connections before it snapshots guests. Unconfirmed operations retain their lease for the executor's detached-session expiry. The journal records their results as unknown. The proxy never repeats them automatically.

Background processes that outlive their executor command have no independent lease. Suspension pauses them. Use another profile for uninterrupted services.

```sh
environment-vm recover swe-demo-b
environment-vm shutdown swe-demo-b
sudo systemctl restart environment-orchestrator
journalctl -u environment-orchestrator
```

Recovery applies to an unexpectedly terminated VM. It cold boots saved files and loses the previous memory session. A matching live orphan VMM blocks recovery. A lifecycle durability failure requires inspection before recovery.

A graceful service restart preserves workspace disks and suspended guest state. Existing harness connections close. Schedule service restarts between tasks.

## Data and later milestones

Keep the runtime directory private and outside Git. RAM checkpoints can contain credentials and unsaved work. Back up the complete suspended workspace, its credentials, metadata, and exact runtime references together.

The current guest image has an immutable Nix store. Add toolchains through its NixOS configuration until durable package installation exists. Automated image migration, maintenance queues, and agent update notices remain later milestones.

Paperclip integration, Browser Use sharing, and NAS backups also remain later work. NAS backups must deduplicate shared image files and repeated workspace data. Do not create an independent full image copy for every agent. See the [PRD](docs/PRD.md) and [update design](docs/updates.md).

## Validation

`nix build` runs the Rust safety tests. [Test instructions](tests/README.md) cover the private API, real guest lifecycle, and resource measurements. Real guest checks can suspend, restore, or crash a selected test workspace.

Measure service PSS separately from guest RAM and checkpoint page cache. Cgroup memory includes reclaimable filesystem cache. It does not represent the service process footprint alone.
