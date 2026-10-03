# Host Codex with a guest workspace

`environment-codex WORKSPACE [codex arguments]` runs the Codex harness on the host. It uses the workspace's Firecracker VM for execution and file operations. The initial implementation uses Codex 0.159.3 without a fork.

The launcher creates a private `CODEX_HOME` for each workspace. The directory contains conversations, user configuration, and a workspace capability. It does not contain the inference API key. The launcher reads the private key file before it starts the harness. It passes the key through `ENVIRONMENT_INFERENCE_API_KEY`.

`environments.toml` selects one remote environment and sets `include_local = false`. Codex sends shell, process, file, patch, image, and project `AGENTS.md` operations to that environment. The inference provider uses `https://ai.h.mattre.id/v1`. The launcher accepts OpenAI model names only.

Bubblewrap gives the host harness a small mount namespace. It exposes the Nix store, system tools, DNS and TLS configuration, and private harness state. It provides an empty directory at the guest workspace path. This directory lets Codex check its initial working directory. It does not contain host or guest workspace files. Bubblewrap mounts shared Codex skills read-only.

The namespace does not expose the host home directory, host workspace files, runtime sockets, or the inference key file. It shares the host network so the harness can use local inference and the execution proxy. This namespace protects host files from an accidental local filesystem fallback. It is not a network sandbox. The VM supplies the execution boundary for workspace tools.

The launcher blocks configuration overrides, alternate providers, host working directories, and host hooks. Browser services, apps, plugins, and subagents remain disabled until their routing is explicit. The shell environment policy uses the guest's core environment. It excludes inference credentials and secret variables. The guest receives no host inference key through normal execution requests.

Codex Code Mode runs on the host inside the same Bubblewrap namespace. It provides the JavaScript tool dispatch that the model expects. Its native workspace tools still use the selected guest executor. The Nix Codex package includes `codex-code-mode-host`.

The launcher mounts resolved TLS certificate files. NixOS certificate directory entries can refer to `/etc/static`. A directory mount alone leaves those links broken in the namespace.

One launcher can own a workspace at a time. A file lock prevents a second harness from writing the same conversation state. Different workspaces use separate state directories.

## Exec-server protocol notes

The guest runs `codex exec-server --listen ws://0.0.0.0:8765`. Its WebSocket path is `/`. Its HTTP readiness path is `/readyz`. The host proxy requires a separate workspace capability.

The protocol uses Codex JSON-RPC envelopes without a `jsonrpc` field. Requests carry `id`, `method`, and optional `params`. The client selects logical process handles. The executor session defines their scope. They do not identify operating system PIDs.

| Operation | Protocol methods |
| --- | --- |
| Initialization | `initialize`, then `initialized` |
| Environment metadata | `environment/info`, `environment/status` |
| Processes | `process/start`, `process/read`, `process/write`, `process/signal`, `process/terminate` |
| Process events | `process/output`, `process/exited`, `process/closed` |
| File contents | `fs/readFile`, `fs/writeFile`, `fs/open`, `fs/readBlock`, `fs/writeBlock`, `fs/close` |
| File paths | `fs/createDirectory`, `fs/getMetadata`, `fs/canonicalize`, `fs/readDirectory`, `fs/walk`, `fs/remove`, `fs/copy` |

`process/start` returns before a long command exits. The proxy must retain an active process lease until `process/exited`. Pending RPC requests also retain a lease. A persistent idle WebSocket must not retain a lease.

The initial proxy retains its guest TCP connection across VM suspension. The host answers WebSocket ping frames locally. It does not use a guest heartbeat timeout during VM suspension. Closing the guest connection starts Codex's 30-second detached-session expiry. Session expiry can invalidate open file and process handles.

Unexpected connection failures must surface as errors. The proxy must not replay an operation with an unknown result. Cold boot or a generation change requires new executor handles. This initial launcher does not implement update maintenance or post-update context notifications.

## Verification

Run `nix shell .#test-tools -c python test_codex.py` to check configuration and mount contracts. Integration checks must run a real Codex session through the proxy. Check guest `AGENTS.md`, shell execution, file patches, images, host-path denial, and credential exclusion. Check that an idle connection can suspend and resume the VM without losing its session.

The initial live check passed on 2026-10-03 with workspace `swe-demo-a`. Codex reported guest hostname `agent-swe-0` and the guest `AGENTS.md` marker. Native `apply_patch` changed a guest file. Native `view_image` returned a guest PNG. Three host credential paths and three inference key variables were absent. A separate RPC read checked the final guest file contents. Evidence is in `~/Docs/firecracker/codex-routing-proof.jsonl` and `codex-routing-tool-calls.json`.

Local source references: `exec-server/src/environment_toml.rs`, `exec-server-protocol/src/protocol.rs`, `exec-server/src/server/session_registry.rs`, `core/src/agents_md.rs`, `core/src/tools/handlers/apply_patch.rs`, and `core/src/tools/handlers/view_image.rs` in the OpenAI Codex repository.

## Shared mail tools

The launcher adds a required `mail` HTTP MCP connection with `search_mail` and `read_mail`. Its temporary loopback capability is the only mail-related secret in the harness configuration. Gmail credentials remain in the host bridge. See [shared mail tools](docs/mail.md) for one-time authentication and limits.
