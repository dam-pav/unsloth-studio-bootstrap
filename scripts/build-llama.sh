#!/usr/bin/env bash
set -Eeuo pipefail

mode="${LLAMA_CPP_MODE:-bundled}"
case "$mode" in
  bundled)
    echo "Using the llama.cpp installation managed by Unsloth"
    exit 0
    ;;
  custom) ;;
  *) echo "ERROR: LLAMA_CPP_MODE must be 'bundled' or 'custom', got '$mode'" >&2; exit 2 ;;
esac

if [[ -z "${LLAMA_CUDA_ARCHITECTURES:-}" ]]; then
  echo "ERROR: LLAMA_CUDA_ARCHITECTURES is required in custom mode (example: 61 or 61;86)" >&2
  exit 2
fi

repo="${LLAMA_CPP_REPO:-https://github.com/unslothai/llama.cpp.git}"
ref="${LLAMA_CPP_REF:-b10079-mix-fb3d4ca}"
unsloth_uid="${UNSLOTH_UID:-1000}"
unsloth_gid="${UNSLOTH_GID:-1000}"
metadata=/out/llama-server.build
build_id="repo=$repo;ref=$ref;arch=$LLAMA_CUDA_ARCHITECTURES;cuda=12.4.1;wrapper=1"

# mktemp creates build directories as root with mode 0700. Repair cached builds
# from older setup images before Studio attempts to traverse the read-only mount.
if [[ -d /out/current ]]; then
  chown "$unsloth_uid:$unsloth_gid" /out/current
fi

if [[ "${LLAMA_SERVER_REBUILD:-0}" != 1 ]] && [[ -x /out/current/llama-server ]] \
    && grep -Fxq "$build_id" "$metadata" 2>/dev/null; then
  echo "Using cached custom llama.cpp build: $build_id"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates git build-essential cmake libcurl4-openssl-dev ninja-build

stage="$(mktemp -d /out/.build.XXXXXX)"
chown "$unsloth_uid:$unsloth_gid" "$stage"
trap 'rm -rf "$stage"' EXIT
git clone --filter=blob:none "$repo" "$stage/source"
if ! git -C "$stage/source" checkout "$ref"; then
  git -C "$stage/source" fetch --depth 1 origin "$ref"
  git -C "$stage/source" checkout FETCH_HEAD
fi
resolved_ref="$(git -C "$stage/source" rev-parse HEAD)"
cmake "$stage/source" -B "$stage/source/build" -G Ninja \
  -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=ON -DLLAMA_CURL=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_ARCHITECTURES="$LLAMA_CUDA_ARCHITECTURES"
cmake --build "$stage/source/build" --target llama-server

mv "$stage/source/build/bin/llama-server" "$stage/source/build/bin/llama-server.bin"
install -m 755 /usr/local/share/llama-server-wrapper "$stage/source/build/bin/llama-server"
ln -s source/build/bin/llama-server "$stage/llama-server"
printf '%s\n%s\n' "$build_id" "commit=$resolved_ref" > "$stage/build-metadata"

rm -rf /out/previous
if [[ -e /out/current ]]; then mv /out/current /out/previous; fi
mv "$stage" /out/current
cp /out/current/build-metadata "$metadata"
trap - EXIT
echo "Installed custom llama.cpp commit $resolved_ref"
