# Rust service validation

These initial measurements used the original 1 GiB SWE images. The current deployment uses 3 GiB limits and 64 GiB sparse disks. See [workspace profiles](workspace-profiles.md) for the resource policy.
Measured on `mbp-agent`, an Intel T2 MacBook Pro running NixOS, on 2026-10-03. The guests use Firecracker 1.16.1 and Codex 0.159.3. Each SWE guest has 1 GiB RAM and two vCPUs.

## Process memory

Both workspaces were suspended with no attached harnesses during these measurements. The Rust sample followed live execution, service restart, and guest recovery checks.

| Measurement | Python deployment | Rust deployment |
| --- | ---: | ---: |
| Resident service processes | 3 | 1 |
| Total process PSS | 98,243 KiB | 5,579 KiB |
| Total process RSS | 151,920 KiB | 8,328 KiB |
| PSS in MiB | 95.9 | 5.45 |

The Rust deployment reduced measured process PSS by 94%. PSS shares the cost of common mapped pages between processes. RSS can count those pages more than once.

These values exclude guest RAM and filesystem cache. The Rust cgroup still held about 2.24 GiB of file cache after checkpoint and disk activity. Cgroup memory alone does not measure service overhead. The kernel can reclaim inactive file cache.

No Rust process CPU ticks occurred during a five-second idle sample. This short sample does not establish throughput or long-term CPU use.

## Lifecycle latency

| Check | Result |
| --- | ---: |
| Separate warm restore to executor readiness | 143 ms |
| First file operation through the retained connection | 237 ms |
| Full 1 GiB snapshot and termination | 1.11 seconds |
| Restore during concurrent workspace checks | 112–268 ms |
| First file operation during concurrent checks | 205–933 ms |

These are local warm-cache measurements. Concurrent snapshot work can delay admission. They do not guarantee cold-cache latency or a 100 ms response for every request.

## Checks

- All 20 Rust safety tests, nine private API tests, and five launcher tests passed.
- Both existing Python-created checkpoints restored through the Rust service.
- Real guest commands, process cancellation, capabilities, and single-writer ownership passed.
- An open executor connection survived suspension and restore with the same boot identity.
- Automatic idle suspension worked while health probes continued.
- A real OpenAI Codex session applied a guest patch and passed three regression tests. Host credential paths and inference variables were absent.
- A service restart checkpointed a running guest. Restore preserved its boot identity and the Codex patch.
- A killed guest required explicit recovery. Recovery preserved saved files and changed its boot identity.

Both workspaces were suspended after the checks. Private evidence remains under `~/Docs/firecracker/` on the deployment host.

## Workspace profile deployment

The 2026-10-03 profile release passed 32 Rust tests, 19 API tests, 10 Claude launcher contract tests, and 26 live dashboard checks. The NixOS deployment has eight slots with 3072 MiB guest limits and 64 GiB sparse disks. Memory overcommit uses a 3072 MiB host reserve plus 512 MiB startup headroom. The three existing workspaces retained all 31 project-file SHA-256 hashes after a clean shutdown, offline disk growth, and cold startup on the new image.

The research guest passed a spreadsheet write/read round trip, DOCX creation, Pandoc HTML conversion, and PDF tool availability. A real Sonnet 5.5 frontend run completed file operations, guest browser interaction, and screenshot inspection with zero tool errors. Frontend cold startup took 6.585 seconds; a subsequent warm restore reached readiness in 99.79 ms, including 80.09 ms for the Firecracker restore API. Full 3 GiB snapshots took 2.02–5.43 seconds in these checks. These are warm-cache measurements, not a cold-cache latency guarantee. All five checked workspaces ended suspended, with no VMM processes.
