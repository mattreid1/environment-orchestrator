# Verification

Use a Nix-provided Python interpreter with `aiohttp`. The API tests require the packaged service binary:

```sh
ENVIRONMENT_ORCHESTRATOR_BIN=/nix/store/.../bin/environment-orchestrator-service python tests/test_api.py -v
```

Each API test creates an isolated state directory, SQLite database, and loopback listener. Fake runners record unexpected startup attempts and exit. A high memory reserve prevents guest admission. The tests do not start Firecracker or change the installed service. A missing binary causes an explicit skip.

`integration.py` and `recovery.py` operate on real guest workspaces. Run those only when guest execution or recovery is intended.

Measure a systemd service after all guests reach the same state:

```sh
python tests/memory.py --service environment-orchestrator.service --implementation rust --guests 'both suspended' --interval 5 --output /tmp/rust-memory.json
```

For an isolated process, use `--pid PID`. Child processes are included by default. Use `--main-only` to exclude them.

Compare aggregate process PSS for runtime overhead. Cgroup `memory.current` also includes file cache and kernel memory. Snapshot file cache can remain after a guest stops. Sum RSS only as a secondary metric because shared mapped pages can be counted more than once. CPU results include the sampling interval and stable process identities. Cgroup CPU totals also cover short-lived child processes.
