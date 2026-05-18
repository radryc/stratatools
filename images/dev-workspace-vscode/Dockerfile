ARG BASE_IMAGE=dev-workspace-vscode-base:latest
ARG OPENVSCODE_SERVER_VERSION=latest
FROM ${BASE_IMAGE} AS monofs-tools

FROM node:22-bookworm-slim AS monofs-vscode-extension

WORKDIR /tmp/monofs-vscode-extension

COPY monofs/vscode-monofs/package*.json ./
RUN npm ci

COPY monofs/vscode-monofs ./
RUN npm run package && \
    npx --yes @vscode/vsce package --no-dependencies --out /tmp/radryc.monofs.vsix

FROM gitpod/openvscode-server:${OPENVSCODE_SERVER_VERSION}

ARG BUILD_VERSION=dev
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG KUBECTL_VERSION=v1.28.4

USER root

ENV DEBIAN_FRONTEND=noninteractive \
        OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
        GOROOT=/usr/local/go \
        GITFS_OVERLAY_DIR=/home/monofs/.monofs/overlay \
        PATH=/home/.openvscode-server/bin:/usr/local/go/bin:/usr/local/bin:${PATH}

RUN apt-get update && \
        apt-get install -y --no-install-recommends \
            bash \
            ca-certificates \
            curl \
            fuse3 \
            git \
            jq \
            make \
            nodejs \
            npm \
            python3 \
            python3-pip \
            ripgrep \
            strace \
            sudo \
            tini \
            util-linux && \
        rm -rf /var/lib/apt/lists/*

COPY --from=monofs-tools /usr/local/bin/monofs-client /usr/local/bin/monofs-client
COPY --from=monofs-tools /usr/local/bin/monofs-admin /usr/local/bin/monofs-admin
COPY --from=monofs-tools /usr/local/bin/monofs-session /usr/local/bin/monofs-session
COPY --from=monofs-tools /usr/local/bin/bazel /usr/local/bin/bazel
COPY --from=monofs-tools /usr/local/go /usr/local/go

RUN arch="$(dpkg --print-architecture)" && \
        case "$arch" in \
            amd64) kubectl_arch=amd64 ;; \
            arm64) kubectl_arch=arm64 ;; \
            *) echo "unsupported architecture for kubectl: $arch" >&2; exit 1 ;; \
        esac && \
        curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" -o /usr/local/bin/kubectl && \
        chmod +x /usr/local/bin/kubectl

RUN useradd --create-home --shell /bin/bash monofs && \
        install -d -m 0755 /etc/sudoers.d && \
        printf 'monofs ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/monofs && \
        chmod 0440 /etc/sudoers.d/monofs && \
    mkdir -p /workspace /mnt/monofs /var/cache/monofs /home/monofs/.monofs/overlay /home/monofs/.kube /home/monofs/.openvscode-server/extensions && \
        chown -R monofs:monofs /workspace /mnt/monofs /var/cache/monofs /home/monofs /home/.openvscode-server && \
        echo 'user_allow_other' >> /etc/fuse.conf && \
        ln -sf /home/.openvscode-server/bin/openvscode-server /usr/local/bin/openvscode-server

COPY --from=monofs-vscode-extension /tmp/radryc.monofs.vsix /tmp/radryc.monofs.vsix

COPY stratatools/images/dev-workspace-vscode/entrypoint.sh /usr/local/bin/dev-workspace-entrypoint.sh

RUN HOME=/home/monofs OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
        openvscode-server --extensions-dir /home/monofs/.openvscode-server/extensions --install-extension /tmp/radryc.monofs.vsix && \
    HOME=/home/monofs OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
        openvscode-server --extensions-dir /home/monofs/.openvscode-server/extensions --list-extensions | grep -Fx 'radryc.monofs' && \
    rm -f /tmp/radryc.monofs.vsix && \
    chown -R monofs:monofs /home/.openvscode-server /home/monofs/.openvscode-server

RUN chmod +x /usr/local/bin/dev-workspace-entrypoint.sh && \
        printf 'build.version=%s\nbuild.commit=%s\nbuild.time=%s\n' "$BUILD_VERSION" "$BUILD_COMMIT" "$BUILD_TIME" > /etc/dev-workspace-buildinfo && \
        printf '%s\n' \
            'export GITFS_OVERLAY_DIR=/home/monofs/.monofs/overlay' \
            'export GOROOT=/usr/local/go' \
            'export PATH=/home/.openvscode-server/bin:/usr/local/go/bin:/usr/local/bin:$PATH' \
            > /etc/profile.d/monofs.sh && \
        chmod 0644 /etc/profile.d/monofs.sh && \
        printf '%s\n' \
            'if [ -f /etc/profile.d/monofs.sh ]; then' \
            '  . /etc/profile.d/monofs.sh' \
            'fi' \
            '' \
            'monofs-setup() {' \
            '  local _out' \
            '  if ! _out=$(monofs-session setup --mount /mnt/monofs); then' \
            '    return 1' \
            '  fi' \
            '  eval "$_out"' \
            '}' \
            '' \
            'monofs-setup 2>/dev/null || echo "[monofs] Run monofs-setup once filesystem mounts to configure build caches."' \
            > /home/monofs/.bashrc && \
        printf '%s\n' \
            'if [ -f "$HOME/.bashrc" ]; then' \
            '  . "$HOME/.bashrc"' \
            'fi' \
            > /home/monofs/.bash_profile && \
        chown monofs:monofs /home/monofs/.bashrc /home/monofs/.bash_profile

LABEL org.opencontainers.image.title="dev-workspace-vscode" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}"

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/dev-workspace-entrypoint.sh"]