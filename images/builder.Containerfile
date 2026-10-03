# Toolchain image for building podman-ubuntu packages.
# Parameterized by Ubuntu release so one Containerfile serves every
# supported version. Language toolchains come from upstream (not the distro
# archives) so the build doesn't depend on how old the release is.
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    sbuild \
    debhelper \
    dh-autoreconf \
    devscripts \
    lintian \
    reprepro \
    dpkg-dev \
    build-essential \
    pkg-config \
    autoconf automake libtool \
    libassuan-dev \
    libbtrfs-dev \
    libcap-dev \
    libglib2.0-dev \
    libgpgme-dev \
    libseccomp-dev \
    libsystemd-dev \
    libyajl-dev \
    python3 \
    git \
    curl \
    ca-certificates \
    jq \
    distro-info-data \
 && rm -rf /var/lib/apt/lists/*

# Go toolchain: install the newest version any tracked podman needs.
# Bump GO_VERSION when a release's notes require newer (watch PR checklist).
ARG GO_VERSION=1.26.1
ARG TARGETARCH
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH:-amd64}.tar.gz" \
    | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust toolchain via rustup (distro rustc is too old on 22.04/24.04 for netavark 2.x).
ARG RUST_VERSION=1.88.0
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain "${RUST_VERSION}" --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
# The Actions runner overrides HOME (e.g. /github/home), which would make
# rustup look for its toolchains in the wrong place. Pin the real locations.
ENV RUSTUP_HOME="/root/.rustup" CARGO_HOME="/root/.cargo"

# sbuild in unshare mode uses this image itself as the build chroot.
RUN sbuild-adduser root || true
