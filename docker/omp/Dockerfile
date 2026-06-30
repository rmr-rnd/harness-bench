FROM debian:bookworm-slim

ARG OMP_VERSION=16.2.8
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) artifact="omp-linux-x64"; sha="1ac3366c9329a70828dd046bd70fb2cd3d32981373fb33df8f882b039de4c42b" ;; \
      arm64) artifact="omp-linux-arm64"; sha="e0ed7637d1acbddcb84455e472f33a6a3afe502d4c03acf68caf32db0f882e31" ;; \
      *) echo "Unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/${artifact}" -o /usr/local/bin/omp; \
    echo "${sha}  /usr/local/bin/omp" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/omp

WORKDIR /workspace

ENTRYPOINT []
