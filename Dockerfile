ARG CUDA_IMAGE=nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
FROM ${CUDA_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git openssh-client python3 python3-pip \
        python3-venv util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/bootstrap.sh /usr/local/bin/unsloth-bootstrap
RUN chmod 755 /usr/local/bin/unsloth-bootstrap

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/unsloth-bootstrap"]
