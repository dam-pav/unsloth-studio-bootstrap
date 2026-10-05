# Investigating Studio stalls on NFS

Studio state may be deliberately hosted on NAS storage for recovery and moving
deployments. This investigation keeps that requirement: it does not relocate
production state or change its journal mode.

## Findings from the affected deployment

- `/workspace/studio` is backed by NFSv4.2, using a hard mount.
- The authentication database reports `journal_mode=wal`.
- Five Python stack snapshots repeatedly show request workers waiting in
  authentication database journal setup, schema reads, or user reads.
- One snapshot catches the server event loop synchronously reading authentication
  state while serving `/`. A database wait there delays otherwise independent
  requests, including liveness. Other snapshots show the event loop waiting
  normally, so the stall is intermittent.
- Separate read-only database probes completed quickly. They do not reproduce
  concurrent application access, writes, or repeated journal setup.

These initial observations identify authentication database operations as a
blocker. The reproduction below isolates NFS locking delays. It does not
establish that migration introduced the underlying storage problem.

## Reproduction on the same NAS export, 2026-10-04

The export was mounted on WSL with NFSv4.2 and the same relevant mount options.
Testing used the initially empty `/media/backup/__DOCKER/unsloth_2` directory,
UID/GID `1004:1002`, and a separate Compose project, `unsloth-nfs-test`, exposed
only at `127.0.0.1:18888`. The existing local stack remained stopped. The test
reused installed Studio 2026.9.14 code read-only; it did not copy production
credentials or application state.

The WSL export denied ownership changes. Test-only overrides pre-created the
NAS directories as `1004:1002` and skipped root chown on those already correctly
owned mount roots. Runtime files retained normal ownership initialization. This
setup adjustment applied to both journal-mode tests; it was not a product change.

The disposable four-worker workload used twelve iterations per worker, one
writer, and FULL synchronization in both modes:

| Storage and mode | Workload elapsed | Observation |
| --- | ---: | --- |
| Local WAL | 1.26 s | All writes committed; integrity OK |
| Local DELETE | 0.21 s | All writes committed; integrity OK |
| NAS WAL | 30.32 s | SELECT stalled 23.87 s; commit stalled 24.42 s |
| NAS DELETE | 1.57 s | All writes committed; integrity OK |
| NAS DELETE, separate repeat | 0.94 s | All writes committed; integrity OK |

A traced WAL repeat captured `fcntl(F_SETLK, F_UNLCK)` calls on `fixture.db-shm`
taking 10.41 and 28.18 seconds, with byte offsets 123 and 124.
These are delays in filesystem unlock operations, not merely SQLite retrying a
busy transaction. SQLite's busy timeout does not bound an individual NFS syscall.
Tracing added overhead; the untraced workload independently reproduced the stall.

The live test then issued three concurrent batches of liveness, HTML, auth status,
model list, knowledge-base list, and Studio update-status requests. The unmodified
application reproduced the reported behavior. Its stack dump caught the event
loop in `auth.storage.get_connection()` while serving HTML, and an auth-status
worker in WAL setup.

For the comparison, a temporary `sitecustomize.py` outside the repository
intercepted connection-level `PRAGMA journal_mode=WAL` requests and substituted
DELETE. This changed the effective policy across the audited initializers without
editing installed Studio code. It is an experiment, not a shipping workaround.
Both auth.db and studio.db reported `journal_mode=delete`, `synchronous=2` (FULL),
and successful quick checks.

| Request | Unmodified WAL observations | DELETE comparison, all HTTP 200 |
| --- | --- | --- |
| Liveness | Up to 34.20 s in completed batches | 0.003–0.005 s |
| HTML `/` | Up to 35.46 s | 0.020–0.023 s |
| Authentication status | Timed out at 90 s | 0.109–0.112 s |
| Model list | 34.66 s, 67.58 s, then 90 s timeout | 0.037–0.044 s |
| Knowledge bases | 56.79 s, then 90 s timeouts | 0.062–0.086 s |
| Studio update status | 56.78 s, then 90 s timeouts | 0.213–0.333 s |

After the DELETE batches, quantization metadata for
`unsloth/Qwen3-0.6B-GGUF` returned HTTP 200 in 0.53 seconds. No model weights were
downloaded. A separate WAL quantization test timed out, but it included login and
did not distinguish which phase timed out, so it is not a quantization endpoint
timing. WAL timeouts mean the client stopped waiting; they do not prove that the
server cancelled the work.

This establishes a reproducible journaling-policy effect on this NAS deployment.
It does not certify every NFS implementation, multi-host access, or recovery
after a NAS/host power failure.

The DELETE experiment also passed a clean container restart: a committed synthetic
marker in studio.db survived, authentication with the test password still worked,
and both databases retained DELETE mode, FULL sync, and successful quick checks.
Repeated post-restart requests completed in 0.004–0.340 seconds; quantization
metadata returned in 0.34 seconds. This verifies ordinary restart persistence,
not power-failure recovery.

## Source audit

The audited authentication `get_connection()` creates the directory, opens the
database, applies directory/file permissions, requests WAL, stats the file, and
reads its schema version on each connection. Those operations all touch storage.
The synchronous authentication call from the async HTML handler allows the wait
to block the event loop.

WAL is also requested by `studio_db`, `credential_secrets`, `rag_db`, `library_db`,
`providers_db`, and `mcp_servers_db`. Changing a database to rollback journaling
once does not override these application requests. The migration helper makes a
clean rollback-mode snapshot; the application subsequently requests WAL again.

SQLite documents [WAL's network filesystem limitation](https://sqlite.org/wal.html)
and [rollback journaling as a possible network-storage mitigation](https://sqlite.org/useovernet.html).
Rollback mode still depends on correct filesystem locking and sync behavior,
and it reduces read/write concurrency. A performance probe cannot certify crash
recovery or multi-host safety.

## Compare disposable databases on local storage and NAS

From a checkout containing the diagnostic script, copy it to the running
container. No image rebuild, dependency installation, or restart is required:

```bash
docker cp scripts/diagnose-sqlite-storage.py \
  unsloth-studio-bootstrap-unsloth-1:/tmp/diagnose-sqlite-storage.py

for journal in WAL DELETE; do
  docker exec --user 1004:1002 unsloth-studio-bootstrap-unsloth-1 \
    timeout 90s /home/unsloth/current/unsloth_studio/bin/python \
    /tmp/diagnose-sqlite-storage.py /tmp /workspace/studio \
    --journal "$journal"
done
```

Use the deployment's actual UID/GID if different. The script creates isolated
temporary directories and synthetic databases, with four concurrent workers
including one writer. It reports stage timings, errors, committed writes, and
integrity checks. Both modes use `synchronous=FULL`; the comparison does not
disable durability to improve timings. It never opens Studio's existing databases.
Each mode gets a separate timeout so a WAL stall does not skip the DELETE probe.
An interrupted run can leave a `.unsloth-sqlite-probe-*` fixture directory behind.

## Capture live syscall wait times

If the disposable probe does not reproduce the stall, capture 25 seconds of
live syscall statistics while opening Settings or model quantizations:

```bash
studio_pid=$(docker exec unsloth-studio-bootstrap-unsloth-1 \
  cat /home/unsloth/current/studio.pid)

docker run --rm \
  --pid=container:unsloth-studio-bootstrap-unsloth-1 \
  --cap-add SYS_PTRACE \
  python:3.12-slim sh -c '
    apt-get update -qq && apt-get install -y -qq strace || exit
    timeout --signal=INT 25s strace -f -c -w \
      -e trace=fcntl,fsync,fdatasync,openat,newfstatat,statx,chmod,fchmod,close,pread64,pwrite64,futex,clock_nanosleep \
      -p "$1"
  ' sh "$studio_pid"
```

The helper installs strace only in itself. `-c` prints aggregate statistics,
without SQL, database bytes, or individual syscall arguments. `-w` measures
elapsed time including waits rather than only kernel CPU time. Attaching a tracer
adds overhead, so interpret the results alongside the untraced measurements.
A timeout exit status is expected after the capture interval.

Long `fcntl` waits point toward filesystem locking; long `fsync`/`fdatasync`
waits toward storage synchronization; metadata waits toward NFS metadata latency.
Repeated sleeps with lock errors can indicate SQLite busy-handler retries.
`futex` includes normal idle thread waits, so a large aggregate alone does not
establish application lock contention. A syscall summary needs to be interpreted
with the Python stacks; it is not a per-file attribution.

## Decision after measurements

If DELETE materially improves the NAS workload, evaluate an application-level
journal policy covering every database initializer, preserving FULL sync in
rollback mode and testing concurrent authentication, settings, chat persistence,
and restart recovery. Keep storage waits out of the event loop as a separate fix.

If both modes stall in NFS operations, changing journal mode alone will not solve
the measured problem. Investigate the NFS server/client waits before introducing
database snapshots or changing the persistence architecture.

## Keeping WAL or shared-memory files locally

The WAL contains committed changes not yet checkpointed into the main database;
it is part of persistent state. Keeping it on disposable local storage would
lose those changes on host loss. Keeping it on durable local storage still splits
recovery across the host and NAS: a NAS snapshot alone no longer captures all
committed database state. Checkpointing can consolidate it, but is not a guarantee
that no new writes enter the WAL immediately afterward.

The `-shm` WAL index is rebuildable, so local shared memory avoids that particular
durability split. SQLite's default filesystem layer places sidecars beside the
database, manages their creation/deletion, and still locks the main database file.
A symlink or file mount is not a robust journal-location policy. SQLite describes
[custom VFS handling for alternate shared memory](https://sqlite.org/wal.html#implementation_of_shared_memory_for_the_wal_index),
and also offers the built-in alternative tested below.

### WAL on NAS with a process-local WAL index: unix-excl

SQLite's [unix-excl VFS](https://sqlite.org/vfs.html) holds an exclusive database
lock and keeps the WAL index in process memory. The database and durable WAL stay
together on NAS. This avoids remote `-shm` lock operations without relocating
committed transactions. It is distinct from `PRAGMA locking_mode=EXCLUSIVE` on
each connection, and it is not the lock-free `unix-none` VFS.

The diagnostic script can compare it directly:

```bash
docker exec --user 1004:1002 unsloth-studio-bootstrap-unsloth-1 \
  timeout 90s /home/unsloth/current/unsloth_studio/bin/python \
  /tmp/diagnose-sqlite-storage.py /workspace/studio \
  --journal WAL --vfs unix-excl --iterations 12
```

On the WSL NAS mount, the synthetic WAL/FULL workload completed in 1.41 seconds,
committed all twelve writes, and passed integrity checks. A second temporary
application policy selected `vfs=unix-excl` through SQLite URI filenames for
connections, without rewriting WAL requests. The same live request batches all
returned HTTP 200:

| Request | unix-excl WAL observations |
| --- | ---: |
| Liveness | 0.004–0.005 s |
| HTML `/` | 0.015–0.020 s |
| Authentication status | 0.085–0.111 s |
| Model list | 0.030–0.045 s |
| Knowledge bases | 0.048–0.078 s |
| Studio update status | 0.193–0.320 s |
| GGUF quantization metadata | 0.298 s |

Both database headers reported WAL format; studio.db retained its WAL on NAS and
neither database had a `-shm` file. Application synchronization policies remained
upstream defaults, including NORMAL for studio.db in WAL mode. The separate
synthetic workload used FULL in all configurations.

A clean restart retained authentication and fast HTTP responses; quantization
metadata returned in 0.32 seconds. A separate SQLite process attempting to read
the open studio.db received `database is locked`, confirming the expected access
restriction. After shutdown, both databases passed quick checks from a separate
process and the synthetic marker still existed. The test stack and its disposable
volumes were removed; the NAS test state was retained. Those temporary policies
did not change the shipping Compose or bootstrap code.

This is a candidate for a single-process database architecture, not a general
NFS compatibility guarantee. Multiple connections in the owning process can
operate, but another process cannot access the database while the exclusive lock
is held. Before selecting it for a production workflow, audit subprocesses, CLI utilities, background workers,
backup tooling, and other hosts that may open the same databases. All relevant
connections must use the intended filesystem policy. Basic HTTP route tests do
not cover training or every Studio workflow, and NFS database locking/sync still
remain part of the durability assumptions.

## Packaged opt-in policies: concurrent users and processes

The bootstrap now offers [optional SQLite modes](sqlite-modes.md), with unmodified
upstream behavior as the default. Packaged-policy tests used a separate Compose
project, `unsloth-sqlite-test`, and fresh state under
`/media/backup/__DOCKER/unsloth_2/policy-delete`. They reused the installed release
read-only and the same UID/GID and ownership workaround described above.

The test host became heavily CPU-contended during startup. A stack capture also
showed PyTorch loading CUDA libraries while holding the GIL, with the server's
event loop idle. Initial login and liveness probes timed out during that warmup.
The test-only Compose override then set upstream's
`UNSLOTH_STUDIO_DISABLE_TORCH_WARM=1` to isolate database workloads. This flag is
not part of the product configuration. These results therefore validate database
transactions, not GPU inference or warmup performance.

The HTTP workload used the owner and three managed accounts. Sixteen threads
created sixteen saved chats concurrently and wrote eight messages per chat,
interleaving liveness requests. Every message was read back and cross-account
thread access returned 404. Public GGUF quantization metadata was also requested.

| Packaged mode and workload | Requests | Median | Maximum | Result |
| --- | ---: | ---: | ---: | --- |
| DELETE, including initial account/password setup | 320 | 0.168 s | 20.204 s | 128 messages saved and verified |
| DELETE, verification after clean restart | 37 | 0.033 s | 1.005 s | Accounts and all messages survived; quantization metadata 0.634 s |
| Exclusive WAL, verification after switching from DELETE | 37 | 0.015 s | 0.931 s | Existing accounts and messages preserved; quantization metadata 0.368 s |
| Exclusive WAL, sixteen additional concurrent chats | 309 | 0.138 s | 0.976 s | Another 128 messages saved and verified; quantization metadata 0.306 s |
| Exclusive WAL, verification after clean restart | 37 | 0.016 s | 0.479 s | Accounts and messages survived; quantization metadata 0.372 s |

The initial DELETE run includes slow account setup on the contended test host;
these rows are functional workload results, not a controlled performance ranking.
During DELETE chat writes, a separate Python process using Studio's own
`storage.studio_db.get_connection()` committed twenty additional writes. Nine
Studio databases, including managed-account databases, reported DELETE and passed
quick checks. In exclusive WAL mode the same accessor in a separate process
returned `database is locked`, while server users continued working. This confirms
both same-process concurrency and the restriction on separate processes.

Switching the same state back to DELETE also passed: both sets of sixteen chats
were verified, totaling 256 messages. Each verification batch had a 0.036-second
median, with quantization metadata returning in 0.440 and 0.452 seconds. Mode
changes required container recreation but no manual database conversion.
After shutdown, all nine databases passed quick checks from a separate process.
The test stack and its disposable volumes were removed; synthetic NAS state was
retained, and temporary exported test credentials were deleted.

The reproducible HTTP client is `tests/sqlite-concurrency.py`. Run it only against
an isolated test deployment; it creates accounts, changes the test owner's
password, and saves chat history. It requires `--allow-test-data` and a private
`--session` file; `--verify-only` checks saved state after restart. No real model
generation or complete training job was run.
