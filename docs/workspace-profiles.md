# Workspace profiles and memory overcommit

Each workspace keeps one profile and one configured slot. The orchestrator records the profile in `state.sqlite`. A service restart preserves the workspace ID, slot, capability, and profile. Existing records migrate to `swe`. A request to change the profile of an allocated workspace fails. Create a separate workspace for a different profile.

Create a workspace without starting its VM:

```sh
environment-vm create product-ui --profile frontend
environment-vm create market-research --profile research
environment-vm create campaigns --profile marketing
environment-vm create accounts --profile sales
```

For a direct Codex launch, set `ENVIRONMENT_PROFILE` before the first allocation:

```sh
ENVIRONMENT_PROFILE=research environment-codex market-research
```

The private `POST /workspaces` API and the LAN dashboard create route accept `{"id":"market-research","profile":"research"}`. A missing profile selects `swe` for a new workspace. The dashboard has a profile picker and shows the bound profile in workspace details. Its observations do not start a VM.

Paperclip discovery reads `adapterConfig.env.ENVIRONMENT_PROFILE`. It accepts a string or a plain binding such as `{"type":"plain","value":"frontend"}`. If the field is absent, `cmo`, `marketing`, and `marketer` select `marketing`; `sales`, `salesperson`, and `cro` select `sales`; `research`, `researcher`, and `analyst` select `research`. Other roles select `swe`. Select `frontend` explicitly for a frontend engineer. Agent discovery does not allocate a slot. Agent execution allocates it on first use. Later role changes preserve an existing workspace profile.

## Host configuration

`ENVIRONMENT_SLOTS` retains the original fields and adds resource and profile data:

```json
[
  {
    "runner":"/nix/store/...-swe-runner",
    "firecracker":"/nix/store/...-firecracker/bin/firecracker",
    "guest_host":"172.30.78.2",
    "memory_mb":3072,
    "profiles":{
      "swe":{"runner":"/nix/store/...-swe-runner","memory_mb":3072},
      "frontend":{"runner":"/nix/store/...-frontend-runner","memory_mb":3072},
      "marketing":{"runner":"/nix/store/...-knowledge-runner","memory_mb":3072},
      "sales":{"runner":"/nix/store/...-knowledge-runner","memory_mb":3072},
      "research":{"runner":"/nix/store/...-knowledge-runner","memory_mb":3072}
    }
  }
]
```

The `runner` and `memory_mb` fields provide the default `swe` image when the profile map omits `swe`. Old slot configurations without `memory_mb` use 1024 MiB. A profile with no configured runner fails before allocation. Each slot must use runner images with that slot's network identity.

With `ENVIRONMENT_MEMORY_OVERCOMMIT=true`, startup waits until Linux `MemAvailable` is at least `ENVIRONMENT_HOST_RESERVE_MB + ENVIRONMENT_STARTUP_HEADROOM_MB`. Defaults are a 3072 MiB reserve and 512 MiB headroom. Existing guest memory is already included in `MemAvailable`; the orchestrator does not subtract their complete guest limits again. Startup is serialized. A queued request can wait for up to 120 seconds and can be cancelled before startup. The headroom permits guest limits to exceed total physical memory; it does not reserve all future guest memory.

With overcommit disabled, the threshold is the host reserve plus the next guest's configured memory limit. Guest CPU limits come from the selected Nix runner. There is no aggregate CPU admission limit.

## Existing checkpoints

A suspended workspace restores its saved runner and Firecracker generation. Changing host configuration does not replace that checkpoint. Cold startup records the selected runner's `memory_mb` in `state.json`. Older saved generations without that field retain the original 1024 MiB bound in status and dashboard data.

To adopt a new image or disk capacity, drain the harness and operations, save a backup, shut down the old guest, grow the stopped disk and filesystem, deploy the new runner, and then cold boot. Shutdown discards the saved RAM session. Persistent guest files remain on disk. Do not resize a disk while its VM runs or while preserving a checkpoint that expects the old drive size.
