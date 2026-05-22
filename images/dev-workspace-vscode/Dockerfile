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

FROM debian:trixie-slim AS litertlm-cpu-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        clang \
        curl \
        git \
        git-lfs \
        python3 && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/bazel \
        https://github.com/bazelbuild/bazelisk/releases/latest/download/bazelisk-linux-amd64 && \
    chmod +x /usr/local/bin/bazel

WORKDIR /src
RUN git clone --depth=1 https://github.com/google-ai-edge/LiteRT-LM.git . && \
    git lfs install --local && \
    git lfs pull

RUN mkdir -p c/litertlm_c_api && cat > c/litertlm_c_api/BUILD <<'EOF'
package(default_visibility = ["//visibility:public"])

cc_binary(
    name = "litertlm_c_cpu",
    linkshared = 1,
    deps = ["//c:engine_cpu"],
)

cc_binary(
    name = "litertlm_c",
    linkshared = 1,
    deps = ["//c:engine"],
)
EOF

RUN --mount=type=cache,target=/root/.cache/bazel \
    set -eux; \
    bazel build //c/litertlm_c_api:litertlm_c_cpu

RUN mkdir -p /out && \
    cp prebuilt/linux_x86_64/*.so /out/ && \
    cp bazel-bin/c/litertlm_c_api/liblitertlm_c_cpu.so /out/

FROM gitpod/openvscode-server:${OPENVSCODE_SERVER_VERSION}

ARG BUILD_VERSION=dev
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG KUBECTL_VERSION=v1.28.4

USER root

ENV DEBIAN_FRONTEND=noninteractive \
        OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
    WORKSPACE_USER=developer \
    WORKSPACE_GROUP=developer \
    WORKSPACE_HOME=/home/developer \
        GOROOT=/usr/local/go \
    GITFS_OVERLAY_DIR=/home/developer/.monofs/overlay \
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
COPY --from=litertlm-cpu-builder /out /opt/litertlm/lib

RUN arch="$(dpkg --print-architecture)" && \
        case "$arch" in \
            amd64) kubectl_arch=amd64 ;; \
            arm64) kubectl_arch=arm64 ;; \
            *) echo "unsupported architecture for kubectl: $arch" >&2; exit 1 ;; \
        esac && \
        curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" -o /usr/local/bin/kubectl && \
        chmod +x /usr/local/bin/kubectl

RUN if ! id "${WORKSPACE_USER}" >/dev/null 2>&1; then \
        groupmod --new-name "${WORKSPACE_GROUP}" openvscode-server && \
        usermod --login "${WORKSPACE_USER}" --home "${WORKSPACE_HOME}" --move-home --gid "${WORKSPACE_GROUP}" openvscode-server; \
    fi && \
    id "${WORKSPACE_USER}" >/dev/null && \
        install -d -m 0755 /etc/sudoers.d && \
        printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${WORKSPACE_USER}" > /etc/sudoers.d/dev-workspace && \
        chmod 0440 /etc/sudoers.d/dev-workspace && \
    mkdir -p /workspace /mnt/monofs /var/cache/monofs "${WORKSPACE_HOME}/.monofs/overlay" "${WORKSPACE_HOME}/.monofs/models" "${WORKSPACE_HOME}/.kube" "${WORKSPACE_HOME}/.openvscode-server/extensions" "${WORKSPACE_HOME}/.ssh" && \
        chown -R "${WORKSPACE_USER}:${WORKSPACE_GROUP}" /workspace /mnt/monofs /var/cache/monofs "${WORKSPACE_HOME}" /home/.openvscode-server /opt/litertlm && \
        chmod 0700 "${WORKSPACE_HOME}/.ssh" && \
        echo 'user_allow_other' >> /etc/fuse.conf && \
        ln -sf /usr/local/bin/monofs-session /usr/local/bin/mfs && \
        ln -sf /home/.openvscode-server/bin/openvscode-server /usr/local/bin/openvscode-server && \
        printf '%s\n' \
            'PasswordAuthentication no' \
            'KbdInteractiveAuthentication no' \
            'ChallengeResponseAuthentication no' \
            'PermitRootLogin no' \
            'PubkeyAuthentication yes' \
            "AuthorizedKeysFile ${WORKSPACE_HOME}/.ssh/authorized_keys" \
            "AllowUsers ${WORKSPACE_USER}" \
            > /etc/ssh/sshd_config.d/dev-workspace.conf

COPY --from=monofs-vscode-extension /tmp/radryc.monofs.vsix /tmp/radryc.monofs.vsix

COPY stratatools/images/dev-workspace-vscode/entrypoint.sh /usr/local/bin/dev-workspace-entrypoint.sh

RUN HOME="${WORKSPACE_HOME}" OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
        openvscode-server --extensions-dir "${WORKSPACE_HOME}/.openvscode-server/extensions" --install-extension /tmp/radryc.monofs.vsix && \
    HOME="${WORKSPACE_HOME}" OPENVSCODE_SERVER_ROOT=/home/.openvscode-server \
        openvscode-server --extensions-dir "${WORKSPACE_HOME}/.openvscode-server/extensions" --list-extensions | grep -Fx 'radryc.monofs' && \
    rm -f /tmp/radryc.monofs.vsix && \
    chown -R "${WORKSPACE_USER}:${WORKSPACE_GROUP}" /home/.openvscode-server "${WORKSPACE_HOME}/.openvscode-server"

RUN chmod +x /usr/local/bin/dev-workspace-entrypoint.sh && \
        printf 'build.version=%s\nbuild.commit=%s\nbuild.time=%s\n' "$BUILD_VERSION" "$BUILD_COMMIT" "$BUILD_TIME" > /etc/dev-workspace-buildinfo && \
        printf '%s\n' \
            "export GITFS_OVERLAY_DIR=${WORKSPACE_HOME}/.monofs/overlay" \
            'export LITERTLM_LIB=/opt/litertlm/lib' \
            "export LITERTLM_MODEL=${WORKSPACE_HOME}/.monofs/models/gemma-4-E2B-it.litertlm" \
            'export GOROOT=/usr/local/go' \
            'export PATH=/home/.openvscode-server/bin:/usr/local/go/bin:/usr/local/bin:$PATH' \
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

LABEL org.opencontainers.image.title="dev-workspace-vscode" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}"

EXPOSE 22
EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/dev-workspace-entrypoint.sh"]