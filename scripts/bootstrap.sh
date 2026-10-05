#!/usr/bin/env bash
set -Eeuo pipefail

state=/home/unsloth
releases="$state/releases"
current="$state/current"
persistent=/workspace/studio
requested="${UNSLOTH_VERSION:-latest}"
uid="${UNSLOTH_UID:-1000}"
gid="${UNSLOTH_GID:-1000}"
sqlite_mode="${UNSLOTH_SQLITE_MODE:-wal}"
sqlite_mode="${sqlite_mode,,}"

case "$sqlite_mode" in
  wal|rollback-journal|wal-exclusive) ;;
  *) echo "ERROR: UNSLOTH_SQLITE_MODE must be wal, rollback-journal, or wal-exclusive" >&2; exit 2 ;;
esac

case "${LLAMA_CPP_MODE:-bundled}" in
  bundled|custom) ;;
  *) echo "ERROR: LLAMA_CPP_MODE must be 'bundled' or 'custom'" >&2; exit 2 ;;
esac
case "$requested" in
  latest|nightly|[0-9]*) ;;
  *) echo "ERROR: UNSLOTH_VERSION must be latest, nightly, or an exact release" >&2; exit 2 ;;
esac

# Inspect the real database mount: DATA_DIR may contain mounts of different types.
# Detection is advisory and must never change the selected mode or prevent boot.
filesystem="$(findmnt -n -o FSTYPE -T "$persistent" 2>/dev/null)" || filesystem=unknown
filesystem="${filesystem##*$'\n'}" # Use the topmost mount if findmnt lists stacked mounts.
filesystem="${filesystem:-unknown}"
printf 'Studio state filesystem: %s (%s); configured SQLite mode: %s\n' "$filesystem" "$persistent" "$sqlite_mode"
case "$filesystem" in
  nfs|nfs4|cifs|smb3|smbfs|sshfs|fuse.sshfs|ceph|fuse.ceph|glusterfs|fuse.glusterfs|lustre|afs|coda|davfs|davfs2|fuse.davfs|fuse.rclone|fuse.smbnetfs)
    if [[ "$sqlite_mode" == wal ]]; then
      cat >&2 <<EOF
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!! WARNING: NETWORK STORAGE WITH SQLITE WAL — SEVERE LATENCY RISK             !!!
!!! Studio state is on $filesystem: $persistent
!!! WAL on network storage can cause MINUTES-LONG STALLS and request timeouts.
!!! STRONGLY RECOMMENDED: set UNSLOTH_SQLITE_MODE=rollback-journal (rollback journaling).
!!! Recreate the Studio container to apply the change. Mode remains: $sqlite_mode
!!! See docs/sqlite-modes.md for configuration and concurrency details.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
EOF
    fi
    ;;
esac

mkdir -p "$releases" "$persistent" /workspace/work /workspace/projects /workspace/.cache /workspace/models
python3 /usr/local/bin/unsloth-migrate-storage \
  --legacy-data /legacy/data --studio "$persistent" --projects /workspace/projects
chown -R "$uid:$gid" "$state" "$persistent" /workspace/work /workspace/projects /workspace/.cache

# Keep saved paths through the former shared-state location usable. The actual
# data is mounted separately, so resetting runtime-home cannot erase it.
if [[ -e "$state/studio-state" && ! -L "$state/studio-state" ]]; then
  echo "ERROR: legacy Studio state found in runtime-home; migrate it to $persistent first" >&2
  exit 1
fi
ln -sfn "$persistent" "$state/studio-state"

# Preserve the old default project paths as aliases into the new project mount.
mkdir -p "$state/Documents/Unsloth Studio"
for relative in Projects Accounts; do
  legacy="$state/Documents/Unsloth Studio/$relative"
  if [[ -e "$legacy" && ! -L "$legacy" ]]; then
    echo "ERROR: legacy projects found at $legacy; migrate them before starting" >&2
    exit 1
  fi
done
ln -sfn /workspace/projects "$state/Documents/Unsloth Studio/Projects"
ln -sfn /workspace/projects/Accounts "$state/Documents/Unsloth Studio/Accounts"

installed_version=""
if [[ -L "$current" && -f "$current/.installed-version" ]]; then
  installed_version="$(<"$current/.installed-version")"
fi

resolve_latest() {
  python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen("https://pypi.org/pypi/unsloth/json", timeout=15) as response:
    print(json.load(response)["info"]["version"])
PY
}

target="$requested"
if [[ "$requested" == latest ]]; then
  if [[ "${UNSLOTH_UPDATE_CHECK:-1}" == 1 || -z "$installed_version" ]]; then
    if ! target="$(resolve_latest)"; then
      if [[ -n "$installed_version" ]]; then
        echo "WARNING: update check failed; continuing with $installed_version" >&2
        target="$installed_version"
      else
        echo "ERROR: cannot resolve the latest Unsloth release" >&2
        exit 1
      fi
    fi
  else
    target="$installed_version"
  fi
elif [[ "$requested" == nightly ]]; then
  if source_ref="$(git ls-remote https://github.com/unslothai/unsloth.git refs/heads/main | awk '{print $1}')" \
      && [[ -n "$source_ref" ]]; then
    target="nightly-${source_ref:0:12}"
  elif [[ "$installed_version" == nightly-* ]]; then
    echo "WARNING: nightly update check failed; continuing with $installed_version" >&2
    target="$installed_version"
    source_ref="${installed_version#nightly-}"
  else
    echo "ERROR: cannot resolve the latest Unsloth nightly commit" >&2
    exit 1
  fi
fi

if [[ -n "$installed_version" && "$target" != "$installed_version" && "${UNSLOTH_AUTO_UPDATE:-1}" != 1 ]]; then
  echo "Unsloth $target is available; automatic updates are disabled, using $installed_version"
  target="$installed_version"
fi

release="$releases/$target"
if [[ ! -x "$release/unsloth_studio/bin/unsloth" ]]; then
  echo "Installing Unsloth Studio release $target"
  rm -rf "$release"
  mkdir -p "$release"
  chown "$uid:$gid" "$release"
  install_log="$state/install-$target.log"

  if ! setpriv --reuid "$uid" --regid "$gid" --clear-groups env \
      HOME="$state" UNSLOTH_STUDIO_HOME="$release" REQUESTED_VERSION="$requested" \
      UNSLOTH_SKIP_AUTOSTART=1 \
      UNSLOTH_SOURCE_REF="${source_ref:-}" \
      bash -c '
        set -Eeuo pipefail
        if [[ "$REQUESTED_VERSION" == nightly ]]; then
          git clone https://github.com/unslothai/unsloth.git "$UNSLOTH_STUDIO_HOME/source"
          git -C "$UNSLOTH_STUDIO_HOME/source" checkout "$UNSLOTH_SOURCE_REF"
          cd "$UNSLOTH_STUDIO_HOME/source"
          ./install.sh --local
        else
          curl -fsSL https://unsloth.ai/install.sh | UNSLOTH_STUDIO_HOME="$UNSLOTH_STUDIO_HOME" sh
          if [[ "$REQUESTED_VERSION" != latest ]]; then
            "$UNSLOTH_STUDIO_HOME/unsloth_studio/bin/python" -m pip install \
              --force-reinstall --no-cache-dir --no-deps "unsloth==$REQUESTED_VERSION"
          fi
        fi
        "$UNSLOTH_STUDIO_HOME/unsloth_studio/bin/unsloth" --version
      ' >"$install_log" 2>&1; then
    echo "ERROR: installation failed; see $install_log" >&2
    rm -rf "$release"
    if [[ -n "$installed_version" && -x "$current/unsloth_studio/bin/unsloth" ]]; then
      echo "Continuing with last working release $installed_version" >&2
      release="$current"
      target="$installed_version"
    else
      exit 1
    fi
  else
    printf '%s\n' "$target" > "$release/.installed-version"
    chown "$uid:$gid" "$release/.installed-version"
  fi
fi

# A CLI executable and --version do not prove that Studio's backend dependencies
# were installed. Check cached releases too, so an incomplete environment can
# recover without deleting the release or its application data.
runtime_log="$state/runtime-$target.log"
if ! setpriv --reuid "$uid" --regid "$gid" --clear-groups env \
    HOME="$state" UNSLOTH_STUDIO_HOME="$release" \
    PATH="$release/unsloth_studio/bin:$state/.local/bin:$PATH" \
    bash -c '
      set -Eeuo pipefail
      python="$UNSLOTH_STUDIO_HOME/unsloth_studio/bin/python"
      if "$python" -c "import fastapi, uvicorn"; then
        exit 0
      fi
      echo "Repairing missing Studio backend dependencies"
      requirements="$("$python" -c '\''import importlib.resources; print(importlib.resources.files("studio") / "backend" / "requirements" / "studio.txt")'\'')"
      test -f "$requirements"
      if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$python" -r "$requirements"
      else
        "$python" -m pip install -r "$requirements"
      fi
      "$python" -c "import fastapi, uvicorn"
    ' >"$runtime_log" 2>&1; then
  echo "ERROR: Studio backend dependency check or repair failed; see $runtime_log" >&2
  tail -n 50 "$runtime_log" >&2
  exit 1
fi

# Studio keeps mutable application data below UNSLOTH_STUDIO_HOME alongside its
# runtime. Keep that data in its own host mount so upgrades do not create
# fresh databases (and, consequently, demand a new password). On the first run,
# adopt data from the previously active release. Studio is not running yet, so
# its SQLite databases are closed while they are moved.
link_persistent_path() {
  local relative="$1"
  local shared="$persistent/$relative"
  local previous=""
  local installed="$release/$relative"

  if [[ -L "$current" ]]; then
    previous="$current/$relative"
  fi

  mkdir -p "$(dirname "$shared")" "$(dirname "$installed")"
  if [[ ! -e "$shared" && ! -L "$shared" ]]; then
    # Existing release links already point at the shared mount. If that mount
    # is empty (a fresh deployment or a changed STUDIO_PATH), moving a dangling
    # link into its own target would create a self-referential symlink.
    if [[ -n "$previous" && -e "$previous" && ! -L "$previous" ]]; then
      mv "$previous" "$shared"
    elif [[ -e "$installed" && ! -L "$installed" ]]; then
      mv "$installed" "$shared"
    fi
  fi

  if [[ -e "$installed" || -L "$installed" ]]; then
    rm -rf -- "$installed"
  fi
  if [[ ! -e "$shared" && ! -L "$shared" ]]; then
    case "$relative" in
      studio.db|share/studio_install_id) : > "$shared" ;;
      *) mkdir -p "$shared" ;;
    esac
  fi
  ln -s "$shared" "$installed"
}

for relative in \
  auth studio.db rag runs exports outputs assets/datasets share/studio_install_id \
  accounts library chat-originals images videos audio transcripts security mcp-oauth-tokens
do
  link_persistent_path "$relative"
done
chown -R "$uid:$gid" "$persistent"

if [[ "$release" != "$current" ]]; then
  ln -sfn "releases/$target" "$state/.current.new"
  mv -Tf "$state/.current.new" "$current"
fi

export HOME="$state"
export UNSLOTH_STUDIO_HOME="$current"
export PATH="$current/unsloth_studio/bin:$state/.local/bin:$PATH"
if [[ "${LLAMA_CPP_MODE:-bundled}" == custom ]]; then
  test -x /opt/llama-server/current/llama-server
  export UNSLOTH_LOCAL_LLAMA_CPP_DIR=/opt/llama-server/current/source
  export UNSLOTH_LLAMA_CPP_PATH=/opt/llama-server/current/source
  export LLAMA_SERVER_PATH=/opt/llama-server/current/llama-server
fi

cd /workspace/work
if [[ "$sqlite_mode" != wal ]]; then
  export UNSLOTH_SQLITE_MODE="$sqlite_mode"
  export PYTHONPATH="/usr/local/lib/unsloth-sqlite-policy${PYTHONPATH:+:$PYTHONPATH}"
  if [[ "$sqlite_mode" == rollback-journal ]]; then
    echo "Studio SQLite mode: $sqlite_mode (rollback journaling / SQLite DELETE; persistent Studio databases only)"
  else
    echo "Studio SQLite mode: $sqlite_mode (persistent Studio databases only)"
  fi
  exec setpriv --reuid "$uid" --regid "$gid" --clear-groups \
    "$current/unsloth_studio/bin/python" /usr/local/lib/unsloth-sqlite-policy/launch.py \
    "$current/unsloth_studio/bin/unsloth" studio -H 0.0.0.0 -p 8000
fi
exec setpriv --reuid "$uid" --regid "$gid" --clear-groups \
  "$current/unsloth_studio/bin/unsloth" studio -H 0.0.0.0 -p 8000
