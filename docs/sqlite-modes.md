# Optional SQLite modes

`UNSLOTH_SQLITE_MODE` selects the policy for SQLite connections whose main
database resolves beneath `/workspace/studio`, including managed account
databases and the release symlinks pointing at that mount.

| Value | Behavior | Concurrent access |
| --- | --- | --- |
| `wal` (default) | Unmodified upstream Studio behavior, including its WAL requests and sync settings. No policy startup hook is loaded. | Upstream SQLite behavior. |
| `rollback-journal` | Forces rollback journaling and at least FULL synchronization. Upstream requests to re-enable WAL or weaken sync are ignored by SQLite's authorizer. | Multiple processes and connections can access the database. SQLite serializes writers and blocks readers during exclusive writes. |
| `wal-exclusive` | Selects SQLite's built-in `unix-excl` VFS while retaining upstream WAL and sync settings. Database and WAL remain together on persistent storage. | Multiple connections in one process can operate. Separate processes are blocked while the database's exclusive process lock is held. |

The alternatives are opt-in compatibility experiments, motivated by the
[NFS investigation](nfs-sqlite-investigation.md). They do not certify arbitrary
NFS implementations or power-loss recovery. Keep the default for local database
storage unless a measured problem justifies another mode.

`rollback-journal` selects SQLite's
[DELETE rollback-journal mode](https://sqlite.org/pragma.html#pragma_journal_mode).
The journal holds the original database pages needed to roll back an unfinished
transaction. SQLite deletes that temporary journal after a successful commit;
the name does not mean it deletes user data. Configuration values are
case-insensitive, so `ROLLBACK-JOURNAL` also works. `delete` is not a configuration value.

## Selecting and changing a mode

Set the variable in your Compose environment or deployment configuration:

```dotenv
UNSLOTH_SQLITE_MODE=rollback-journal
```

Rebuild/pull a bootstrap image containing this feature, then recreate the Studio
container through your usual deployment mechanism. Changing a container's shell
environment does not change its running database connections. Switching policies
requires stopping every process using the same databases and restarting Studio.
SQLite handles the journal transition when the connections reopen; do not delete
WAL files manually. Returning to `wal` removes the hook and lets upstream request
WAL normally again.

At every startup, bootstrap inspects the filesystem of `/workspace/studio`, the
actual persistent database mount derived from `DATA_DIR` and `UNSLOTH_STUDIO_PATH`.
Known network filesystems (including NFS, SMB/CIFS, SSHFS, Ceph, GlusterFS and
Lustre) with `wal` selected produce a conspicuous warning about severe stalls
and strongly recommend `UNSLOTH_SQLITE_MODE=rollback-journal`. The check reads mount
metadata; it does not open or modify databases, change the selected mode, or
block startup when detection is unavailable. `rollback-journal` and
`wal-exclusive` do not produce that WAL warning.

An unrecognized type is not proof of local storage. Container filesystem layers
and host sharing can hide the underlying storage type. The logged type is the
filesystem visible to the container; inspect the host mount when necessary.
There is no change to local-storage users who leave the variable unset.
Unrecognized mode values fail validation before migration or installation.

## Isolation and compatibility

Alternative modes load a standard-library Python startup hook for Studio and
Python children that inherit its environment. Installed Studio source is not
edited. Connections outside the Studio state mount, ordinary project databases,
and in-memory databases retain their normal behavior. URI options such as
`mode=ro`, connection timeout, transaction settings, and thread checks are
preserved. Symlinks resolving outside the state directory are outside policy scope.

`rollback-journal` uses SQLite's parsed PRAGMA authorization events, covering connection
methods, cursors, and scripts without rewriting SQL text. Ignored setting
statements return no rows; reading `PRAGMA journal_mode` still reports the actual
mode. Application authorizers are chained so their denials remain in effect.
Custom connection factories must be `sqlite3.Connection` subclasses in this mode.
Policy selection is a compatibility mechanism, not a security sandbox.

`wal-exclusive` is distinct from disabling locks. Its writable connections use a
process-local WAL index and normally create no `-shm` file. Read-only connections
can still create/use shared-memory sidecars; their access mode is not silently
upgraded to writable. Separate CLI, training, inspection, or backup processes may
fail with `database is locked`. Multiple users or prompt threads in the same
server process do not, by themselves, imply a process conflict.

A child using isolated Python startup (`-I`/`-S`), dropping `PYTHONPATH`, another
SQLite binding, or opening databases directly from native code can bypass the
Python hook. Attached databases also require care: DELETE authorization applies
to statements on the selected connection, but initialization configures only its
main database. Workflows using those mechanisms need separate compatibility
testing before selecting an alternative.

The default launcher path stays unchanged. The alternative launcher checks that
the hook loaded; it refuses to start if Python silently ignored a startup-hook
error. This avoids falling back to upstream WAL while reporting another policy.

## Validation

The policy tests exercise generated file/URI names, path confinement, default
identity, cursor/script PRAGMAs, application authorization, connection options,
concurrent writes, and separate-process access. Run them with:

```bash
python3 -m unittest discover -s tests -p test_sqlite_policy.py
```

See the NFS investigation for measured latency and workload results. Database
concurrency tests and saved chat-thread tests do not substitute for testing
actual GPU inference or a complete training job.

Both packaged alternatives passed sixteen concurrent saved-chat threads across
four accounts on the test NAS, message/account isolation checks, clean restarts,
and switching between modes. DELETE also allowed a separate backend process to
commit while HTTP writes were running; exclusive WAL blocked that process as
designed. For workflows needing separate database processes, test `rollback-journal` first.
