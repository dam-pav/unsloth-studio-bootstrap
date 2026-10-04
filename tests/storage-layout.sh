#!/usr/bin/env bash
# Exercise the actual launcher without downloads, a GPU, or a running container.
set -Eeuo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
mkdir -p "$fixture/bin" "$fixture/workspace/models"
sed -e "s|/home/unsloth|$fixture/home|g" \
    -e "s|/workspace|$fixture/workspace|g" \
    -e "s|/legacy/data|$fixture/legacy-data|g" \
    -e "s|/usr/local/bin/unsloth-migrate-storage|$root/scripts/migrate-storage.py|g" \
    "$root/scripts/bootstrap.sh" > "$fixture/bootstrap.sh"

# Replace privilege changes and network installation only. All state management,
# release selection, aliases and linking run through the production launcher.
cat > "$fixture/bin/chown" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$fixture/bin/setpriv" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
install=0
studio=""
for argument in "$@"; do
  case "$argument" in
    REQUESTED_VERSION=*) install=1 ;;
    UNSLOTH_STUDIO_HOME=*) studio="${argument#*=}" ;;
  esac
done
if [[ "$install" == 1 ]]; then
  [[ "${FAIL_INSTALL:-0}" != 1 ]] || exit 1
  mkdir -p "$studio/unsloth_studio/bin" "$studio/auth"
  printf 'installer seed\n' > "$studio/auth/credential"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$studio/unsloth_studio/bin/unsloth"
  chmod +x "$studio/unsloth_studio/bin/unsloth"
fi
SH
chmod +x "$fixture/bin/"*

launch() {
  PATH="$fixture/bin:$PATH" UNSLOTH_VERSION="$1" LLAMA_CPP_MODE=bundled \
    bash "$fixture/bootstrap.sh"
}

mkdir -p "$fixture/legacy-data/home/studio-state/auth"
printf 'legacy password\n' > "$fixture/legacy-data/home/studio-state/auth/credential"
launch 1
[[ "$(cat "$fixture/home/current/auth/credential")" == 'legacy password' ]]
[[ -f "$fixture/workspace/studio/.storage-layout-v2" ]]
printf 'saved password\n' > "$fixture/workspace/studio/auth/credential"
printf 'chat history\n' > "$fixture/workspace/studio/studio.db"
printf 'uploaded asset\n' > "$fixture/workspace/studio/library/document"
printf 'project file\n' > "$fixture/workspace/projects/project"
printf 'model weights\n' > "$fixture/workspace/models/model"

verify_saved_data() {
  local active="$fixture/home/current"
  [[ "$(cat "$active/auth/credential")" == 'saved password' ]]
  [[ "$(cat "$active/studio.db")" == 'chat history' ]]
  [[ "$(cat "$active/library/document")" == 'uploaded asset' ]]
  [[ "$(cat "$fixture/home/studio-state/auth/credential")" == 'saved password' ]]
  [[ "$(cat "$fixture/home/Documents/Unsloth Studio/Projects/project")" == 'project file' ]]
  [[ "$(cat "$fixture/workspace/models/model")" == 'model weights' ]]
}

launch 1 # Ordinary restart.
verify_saved_data
launch 2 # A new install must not replace existing credentials with its seed.
verify_saved_data
FAIL_INSTALL=1 launch 3 # Failed update falls back to the working release.
[[ "$(cat "$fixture/home/current/.installed-version")" == 2 ]]
verify_saved_data

rm -rf "$fixture/home" # Removing disposable runtime storage preserves user data.
launch 2
verify_saved_data

# Selecting an empty Studio host directory while reusing the runtime must create
# fresh state, not move the old release's dangling links into their own targets.
mv "$fixture/workspace/studio" "$fixture/workspace/previous-studio"
mkdir "$fixture/workspace/studio"
launch 2
[[ -d "$fixture/home/current/auth" ]]
[[ -f "$fixture/home/current/studio.db" ]]
[[ ! -L "$fixture/workspace/studio/auth" ]]
[[ ! -L "$fixture/workspace/studio/studio.db" ]]
[[ "$(cat "$fixture/workspace/previous-studio/auth/credential")" == 'saved password' ]]

# Adopt an asset directory left in the old release by a previous bootstrap.
rm "$fixture/home/current/images"
rmdir "$fixture/workspace/studio/images"
mkdir "$fixture/home/current/images"
printf 'legacy image\n' > "$fixture/home/current/images/image"
launch 4
[[ "$(cat "$fixture/home/current/images/image")" == 'legacy image' ]]
[[ "$(cat "$fixture/workspace/studio/images/image")" == 'legacy image' ]]

printf 'Storage layout: restart, upgrade, fallback, runtime reset, fresh state and adoption passed.\n'
