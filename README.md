# Unsloth Studio Bootstrap

A GPU-enabled, self-updating Unsloth Studio deployment. The container image holds
stable CUDA and operating-system dependencies; Studio itself is installed into
persistent, versioned directories so frequent upstream releases do not require a
new image.

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
The included GitHub Actions workflow rebuilds the runtime image weekly with
`pull: true`, so an updated CUDA base image is incorporated automatically. Pushes
to `main` and manual workflow runs also publish to GHCR.

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
