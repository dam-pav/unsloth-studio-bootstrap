#!/usr/bin/env bash
set -Eeuo pipefail

state=/home/unsloth
releases="$state/releases"
current="$state/current"
persistent="$state/studio-state"
requested="${UNSLOTH_VERSION:-latest}"
uid="${UNSLOTH_UID:-1000}"
gid="${UNSLOTH_GID:-1000}"

case "${LLAMA_CPP_MODE:-bundled}" in
  bundled|custom) ;;
  *) echo "ERROR: LLAMA_CPP_MODE must be 'bundled' or 'custom'" >&2; exit 2 ;;
esac
case "$requested" in
  latest|nightly|[0-9]*) ;;
  *) echo "ERROR: UNSLOTH_VERSION must be latest, nightly, or an exact release" >&2; exit 2 ;;
esac

mkdir -p "$releases" "$persistent" /workspace/work /workspace/.cache /workspace/models
chown -R "$uid:$gid" "$state" /workspace/work /workspace/.cache

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

# Studio keeps mutable application data below UNSLOTH_STUDIO_HOME alongside its
# runtime. Keep that data outside versioned releases so upgrades do not create
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
    if [[ -n "$previous" && ( -e "$previous" || -L "$previous" ) ]]; then
      mv "$previous" "$shared"
    elif [[ -e "$installed" || -L "$installed" ]]; then
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
  auth studio.db rag runs exports outputs assets/datasets share/studio_install_id
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
exec setpriv --reuid "$uid" --regid "$gid" --clear-groups \
  "$current/unsloth_studio/bin/unsloth" studio -H 0.0.0.0 -p 8000
