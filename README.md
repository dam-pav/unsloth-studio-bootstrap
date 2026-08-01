# Unsloth Studio Bootstrap

This repository provides a GPU-enabled, self-updating Unsloth Studio deployment
for users who need newer Studio releases than the official GHCR image currently
provides. That image severely lags behind upstream development, making recent
features and fixes unavailable to image-based deployments.

To close that gap without rebuilding an entire image for every Studio release,
this deployment separates the stable CUDA and operating-system dependencies
from Studio itself. Studio is installed into persistent, versioned directories
and can update at container startup, while retaining a known working release for
rollback.

## Requirements

- Docker Engine with Compose v2
- NVIDIA Container Toolkit
- A compatible NVIDIA driver
- An existing macvlan network when using transparent LAN mode

## Configure

```bash
cp .env.example .env
```

`DATA_DIR` and `MODELS_PATH` are required. Compose stops with an explanatory
error when either is missing. Persistent subdirectory names have safe defaults.

## Environment variables

A checkmark means the variable must be set for the deployment mode shown. All
other variables are optional and use the listed default when unset.

| Variable | Required | Default | Role and impact |
| --- | :---: | --- | --- |
| `DATA_DIR` | ✓ | none | Host directory containing persistent work, cache, home, and llama.cpp data. |
| `MODELS_PATH` | ✓ | none | Host directory mounted as the shared model store. |
| `UNSLOTH_WORK_PATH` |  | `work` | Work subdirectory beneath `DATA_DIR`; changing it selects a different persistent workspace. |
| `UNSLOTH_CACHE_PATH` |  | `cache` | Cache subdirectory beneath `DATA_DIR`. |
| `UNSLOTH_HOME_PATH` |  | `home` | Home and versioned Studio release subdirectory beneath `DATA_DIR`. |
| `UNSLOTH_LLAMA_PATH` |  | `llama` | Subdirectory beneath `DATA_DIR` used for cached custom llama.cpp builds. |
| `UNSLOTH_UID` |  | `1000` | UID that owns persistent files and runs Studio inside the container. |
| `UNSLOTH_GID` |  | `1000` | GID that owns persistent files and runs Studio inside the container. |
| `UNSLOTH_VERSION` |  | `latest` | Selects the stable release, an exact version, or `nightly`; see the release policy below. |
| `UNSLOTH_UPDATE_CHECK` |  | `1` | Set to `0` to skip resolving a newer stable release when an installation already exists. |
| `UNSLOTH_AUTO_UPDATE` |  | `1` | Set to `0` to keep running the installed release when a newer target is found. |
| `LLAMA_CPP_MODE` |  | `bundled` | Uses Unsloth's managed llama.cpp; `custom` enables the build and settings below. |
| `LLAMA_CPP_REPO` |  | Unsloth llama.cpp repository | Git repository cloned for a custom build. |
| `LLAMA_CPP_REF` |  | `b10079-mix-fb3d4ca` | Branch, tag, or commit built in custom mode; changing it invalidates the build cache. |
| `LLAMA_CUDA_ARCHITECTURES` | ✓ when `custom` | none | Semicolon-separated CUDA compute capabilities to compile, such as `61` for Tesla P40. |
| `LLAMA_SERVER_REBUILD` |  | `0` | Set to `1` to rebuild llama.cpp even when the cached build metadata matches. |
| `LLAMA_SERVER_FLASH_ATTN` |  | `off` | Custom mode only; bundled mode ignores this variable. The wrapper disables llama.cpp flash attention by default for compatibility with this repository's older Pascal/Tesla P40 deployment. Enable it only when the custom llama.cpp build and target GPU support it. |
| `LLAMA_SERVER_FIT` |  | `off` | Custom mode only; bundled mode ignores this variable. llama.cpp's `--fit on` automatically adjusts otherwise-unset GPU-layer allocation, tensor split, tensor overrides, and an implicit context size so the model fits available device memory. `off` disables that automatic tuning. Set this variable to `on` to enable it, or `keep` to stop the wrapper from forcing a value and use llama.cpp's default (currently `on`). |
| `UNSLOTH_GPU_DEVICES` |  | `all` | Controls which NVIDIA GPUs are visible to the setup and Studio containers. |
| `HF_TOKEN` |  | empty | Hugging Face access token for gated or private model downloads. |
| `TZ` |  | `Etc/UTC` | Sets the local time used by Studio and its logs. Match it to the host or operator time zone (for example, `Europe/Ljubljana`) to make timestamps easier to interpret and correlate. |
| `UNSLOTH_BIND_ADDRESS` |  | `127.0.0.1` | Host interface used by the NAT/local web endpoint. |
| `UNSLOTH_PORT` |  | `8000` | Host port used by the NAT/local web endpoint. |
| `MACVLAN_NETWORK` |  | `macvlan_net` | Name of the existing Docker macvlan network used by the override. |
| `UNSLOTH_IPV4_ADDRESS` | ✓ with macvlan | none | Static LAN address assigned to the web container. |
| `UNSLOTH_MAC_ADDRESS` | ✓ with macvlan | none | Static MAC address assigned to the web container. |
| `UNSLOTH_IMAGE` |  | `unsloth-studio-bootstrap:local` | Runtime image name or registry reference; affects whether Compose builds or pulls the deployment image. |
| `LLAMA_SETUP_IMAGE` |  | `ghcr.io/dam-pav/unsloth-studio-bootstrap-llama-setup:latest` | Helper image containing the custom llama.cpp build scripts. |
| `UNSLOTH_WEB_IMAGE` |  | `ghcr.io/dam-pav/unsloth-studio-bootstrap-web:latest` | Nginx image containing the Studio reverse-proxy configuration. |
| `UNSLOTH_CUDA_IMAGE` |  | `nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04` | NVIDIA CUDA base used to build the runtime image. When NVIDIA publishes an update you want to adopt, set this to the desired compatible `nvidia/cuda` `cudnn-devel` Ubuntu tag, verify that the host driver supports its CUDA version, then run `docker compose build --pull unsloth` followed by `docker compose up -d` to rebuild and deploy it. |
| `UNSLOTH_HEALTH_START_PERIOD` |  | `10m` | Grace period before failed Studio health checks count against the container. |

## Run locally or in WSL (NAT)

```bash
docker compose up -d --build
```

Studio is available at `http://127.0.0.1:8000` by default. Change
`UNSLOTH_BIND_ADDRESS` to expose it on another host interface.

## Run with a transparent LAN address (macvlan)

Set `UNSLOTH_IPV4_ADDRESS`, `UNSLOTH_MAC_ADDRESS`, and optionally
`MACVLAN_NETWORK`, then run:

```bash
docker compose -f docker-compose.yml -f docker-compose.macvlan.yml up -d --build
```

The override removes the NAT port mapping. Docker hosts normally cannot contact
their own macvlan containers without an additional host-side macvlan interface.

## Deploy from Git with Portainer

Use `docker-compose.yml` as the Compose path. Add
`docker-compose.macvlan.yml` for transparent LAN mode and
`docker-compose.portainer.yml` to use the published runtime image instead of
building it through Portainer. The deployment images contain their scripts and
Nginx configuration, so the stack does not bind-mount files from Portainer's
Git checkout onto the Docker endpoint.

## Studio release policy

The default follows the latest stable PyPI release and checks once whenever the
container starts:

```dotenv
UNSLOTH_VERSION=latest
UNSLOTH_UPDATE_CHECK=1
UNSLOTH_AUTO_UPDATE=1
```

To freeze or roll back temporarily:

```dotenv
UNSLOTH_VERSION=2026.5.7
```

For the GitHub main branch:

```dotenv
UNSLOTH_VERSION=nightly
```

Installations live under `${DATA_DIR}/${UNSLOTH_HOME_PATH}/releases`. A new
release becomes `current` only after the installer completes and the launcher
passes a smoke check. On failure, the previous release continues to run and the
failure is recorded in `install-<version>.log`.

Exact-version mode uses the current Studio installer and then pins the Unsloth
Core wheel. Upstream does not currently publish the whole Studio installer as a
versioned artifact, so an old Core version may not always be compatible with the
latest Studio scaffold. Keep a known-working container image and data backup for
long-lived production rollback requirements.

## llama.cpp policy

`llama.cpp` provides the native inference server used to run exported models.
The managed build supplied by Unsloth is the simplest choice for supported
hardware, but a custom build is useful when its precompiled CUDA targets do not
include an older GPU, or when a deployment must pin a known llama.cpp revision.
Compiling locally makes the required CUDA architecture explicit and produces a
server binary optimized for the GPUs that will actually run it.

Our own deployment, for example, runs on older NVIDIA Tesla P40 GPUs. These are
Pascal cards with CUDA compute capability 6.1, so we build llama.cpp ourselves
with `LLAMA_CUDA_ARCHITECTURES=61` instead of relying on a managed binary that
may not include Pascal support.

Use Unsloth's managed build (default):

```dotenv
LLAMA_CPP_MODE=bundled
```

Or compile a custom CUDA build:

```dotenv
LLAMA_CPP_MODE=custom
LLAMA_CPP_REF=b10079-mix-fb3d4ca
LLAMA_CUDA_ARCHITECTURES=61
```

Multiple CUDA architectures can be separated with semicolons, for example
`61;86`. The cache key includes the repository, ref, CUDA version, architecture
list, and wrapper version. A replacement is built in staging and the previous
successful build is retained as `previous`.

## Updates and image rebuilds

Studio updates are handled at startup independently of the container image.
The included GitHub Actions workflow rebuilds the runtime, llama.cpp setup, and
web images weekly with `pull: true`, so updated base images are incorporated
automatically. Relevant pushes to `main` and manual workflow runs also publish
all three images to GHCR.

A separate weekly workflow checks Docker Hub for newer NVIDIA CUDA releases in
the currently selected CUDA major and `cudnn-devel` Ubuntu image family. When it
finds one, it first test-builds the runtime image and then opens a pull request
updating the defaults in the Dockerfile, Compose file, and this README. New CUDA
major versions are intentionally not selected automatically because they can
require a newer host driver or other compatibility changes.

Useful commands:

```bash
docker compose logs -f unsloth
docker compose ps
docker compose build --pull unsloth
docker compose up -d
```

Back up `DATA_DIR` before removing old release directories. Do not expose Studio
to an untrusted network without configuring authentication inside Studio or an
authenticating reverse proxy.
