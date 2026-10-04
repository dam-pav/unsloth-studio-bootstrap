# Storage layout v1 to v2 migration

Migration is enabled by default. Upgrade the runtime image and Compose file
together, then redeploy normally. Compose stops the previous Studio container
before its replacement starts; do not run another Studio instance against the
same legacy data during migration. Back up `DATA_DIR` before upgrading.

The first startup reads the old host data through a read-only `/legacy/data`
mount. It discovers `home/studio-state`, falling back to `home/current` for older
installations, and copies durable state into the new Studio directory. It also
copies the default project workspaces and Hugging Face CLI credentials from the
old work directory. Installed releases, binaries and download/build caches are
excluded. SQLite databases are staged with their journal sidecars and checked;
committed WAL records are included in clean database snapshots.

If your old `.env` contains a custom `UNSLOTH_HOME_PATH`, keep it for the first
upgrade. This obsolete variable is used only to locate legacy data, with `home`
as the default. `UNSLOTH_WORK_PATH` continues to identify the old and new work
mount. The new `UNSLOTH_STUDIO_PATH` and `UNSLOTH_PROJECTS_PATH` default to
`studio-state` and `projects`; setting them is optional.

Migration checks all destination conflicts before publishing data and refuses
to overwrite different files. An interrupted copy can be retried: identical
files are accepted and missing files are copied. A `.storage-layout-v2` marker
in the Studio directory is written only after completion. Later startups skip
legacy migration, so old credentials cannot replace passwords changed in Studio.
The source files are never deleted or modified. Allow enough free disk space
for staging and copying user data; the first upgrade also reinstalls the runtime
and, in custom mode, rebuilds llama.cpp.

After checking your login, settings, history, assets, projects and models, remove
`UNSLOTH_HOME_PATH`, `UNSLOTH_CACHE_PATH` and `UNSLOTH_LLAMA_PATH` from `.env`.
You can then remove the old host `home`, `cache` and `llama` directories. Keep
`DATA_DIR`, `UNSLOTH_WORK_PATH` and `MODELS_PATH`. The discovery mount remains
read-only and does not recreate those legacy directories.

If migration reports a conflict or a corrupt database, Studio stays stopped.
Resolve the reported paths and restart. If you recover the data manually, keep
Studio stopped until you have verified the restored databases, credentials,
assets and projects. Then record completion in the Studio directory before
restarting (replace the path with your configured host directory):

```bash
printf '2\n' > /srv/unsloth/studio-state/.storage-layout-v2
```

This uses the same marker as automatic migration; no environment switch is
needed. Custom asset or project locations outside the old default directories still require their
own mounts or manual migration. References into an old numbered release may
need updating inside Studio; the former shared-state and default project paths
remain compatible aliases.

For Portainer, re-pull the runtime image along with the Compose update. The same
migration runs when the replacement Studio container starts.

[Back to the README](../README.md).
