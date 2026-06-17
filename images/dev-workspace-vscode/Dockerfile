ARG BASE_IMAGE=dev-workspace-opencode-base:latest
FROM ${BASE_IMAGE} AS monofs-tools

FROM node:22-bookworm-slim

ARG BUILD_VERSION=dev
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG KUBECTL_VERSION=v1.28.4

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    WORKSPACE_USER=developer \
    WORKSPACE_GROUP=developer \
    WORKSPACE_HOME=/home/developer \
    GOROOT=/usr/local/go \
    GITFS_OVERLAY_DIR=/home/developer/.monofs/overlay \
    PATH=/usr/local/go/bin:/usr/local/bin:${PATH}

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fuse3 \
        git \
        jq \
        make \
        openssh-server \
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

RUN npm install -g opencode-ai

RUN if id "${WORKSPACE_USER}" >/dev/null 2>&1; then \
        :; \
    else \
        groupadd "${WORKSPACE_GROUP}" && \
        useradd --gid "${WORKSPACE_GROUP}" --home "${WORKSPACE_HOME}" --shell /bin/bash --create-home "${WORKSPACE_USER}"; \
    fi && \
    install -d -m 0755 /etc/sudoers.d && \
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${WORKSPACE_USER}" > /etc/sudoers.d/dev-workspace && \
    chmod 0440 /etc/sudoers.d/dev-workspace && \
    mkdir -p /workspace /mnt/monofs /var/cache/monofs "${WORKSPACE_HOME}/.monofs/overlay" "${WORKSPACE_HOME}/.kube" "${WORKSPACE_HOME}/.config/opencode" "${WORKSPACE_HOME}/.ssh" && \
    chown -R "${WORKSPACE_USER}:${WORKSPACE_GROUP}" /workspace /mnt/monofs /var/cache/monofs "${WORKSPACE_HOME}" && \
    chmod 0700 "${WORKSPACE_HOME}/.ssh" && \
    echo 'user_allow_other' >> /etc/fuse.conf && \
    ln -sf /usr/local/bin/monofs-session /usr/local/bin/mfs && \
    printf '%s\n' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'ChallengeResponseAuthentication no' \
        'PermitRootLogin no' \
        'PubkeyAuthentication yes' \
        "AuthorizedKeysFile ${WORKSPACE_HOME}/.ssh/authorized_keys" \
        "AllowUsers ${WORKSPACE_USER}" \
        > /etc/ssh/sshd_config.d/dev-workspace.conf

COPY stratatools/images/dev-workspace-vscode/entrypoint.sh /usr/local/bin/dev-workspace-entrypoint.sh

RUN chmod +x /usr/local/bin/dev-workspace-entrypoint.sh && \
    printf 'build.version=%s\nbuild.commit=%s\nbuild.time=%s\n' "$BUILD_VERSION" "$BUILD_COMMIT" "$BUILD_TIME" > /etc/dev-workspace-buildinfo && \
    printf '%s\n' \
        "export GITFS_OVERLAY_DIR=${WORKSPACE_HOME}/.monofs/overlay" \
        'export GOROOT=/usr/local/go' \
        'export PATH=/usr/local/go/bin:/usr/local/bin:$PATH' \
        > /etc/profile.d/monofs.sh && \
    chmod 0644 /etc/profile.d/monofs.sh && \
    printf '%s\n' \
        'if [ -f /etc/profile.d/monofs.sh ]; then' \
        '  . /etc/profile.d/monofs.sh' \
        'fi' \
        '' \
        'if _monofs_setup=$(monofs-session setup --mount /mnt/monofs 2>/dev/null); then' \
        '  eval "$_monofs_setup"' \
        'else' \
        '  echo "[monofs] monofs-session setup --mount /mnt/monofs failed; rerun it after the filesystem mounts."' \
        'fi' \
        'unset _monofs_setup' \
        > "${WORKSPACE_HOME}/.bashrc" && \
    printf '%s\n' \
        'if [ -f "$HOME/.bashrc" ]; then' \
        '  . "$HOME/.bashrc"' \
        'fi' \
        > "${WORKSPACE_HOME}/.bash_profile" && \
    chown "${WORKSPACE_USER}:${WORKSPACE_GROUP}" "${WORKSPACE_HOME}/.bashrc" "${WORKSPACE_HOME}/.bash_profile"

LABEL org.opencontainers.image.title="dev-workspace-opencode" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}"

EXPOSE 22
EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/dev-workspace-entrypoint.sh"]
