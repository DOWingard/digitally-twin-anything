# The command line tool `dta` and the docker client it drives.
#
# The plugin's launcher (bin/dta) runs this image on a workspace: the
# workspace mounted at its own path, the host's docker socket, the host's
# network, the calling user's uid and gid. Compose files and build contexts
# the toolkit hands to docker are therefore resolved by the host's daemon at
# the paths the toolkit sees.
#
# The launcher builds it the first time it needs it, from the plugin's root
# (the repository's root) as context; by hand:
#
#   docker build -f docker/toolkit.Dockerfile -t dta-toolkit:0.1.0 .
#
# What the build may read is limited by docker/toolkit.Dockerfile.dockerignore;
# it needs only committed files. What it pulls: the two base images, by
# digest; the third-party packages, by the lock file's versions and hashes;
# the installer, by version; the build backend of the project's own packages
# (hatchling) at its newest version, the one thing not pinned. The image
# holds no secret and no store or source driver: the toolkit reaches a twin's
# store and sources only through the twin's own containers, and reads a
# workspace's .env from the mounted workspace when it runs.

# The docker client and its compose and buildx plugins are static binaries.
FROM docker:29.9.3-cli@sha256:5a7af0dcb0e46c803efe9b45371dd21cb500e5b4453250394ac6f06d49b6e6f1 AS docker-cli

FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# The container runs as whoever calls the launcher, a uid this image does not
# know, with nothing writable but the mounted workspace and /tmp; the home
# directory and the docker client's configuration are therefore under /tmp.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    DOCKER_CONFIG=/tmp/.docker

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli \
    /usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-buildx \
    /usr/local/libexec/docker/cli-plugins/

# Two layers. The first holds the third-party packages and is made from the
# lock file and the packages' metadata alone, so it is built once per lock
# file; the second holds this project's own packages and is all that a change
# to their code rebuilds. Nothing is copied for the build, only mounted; the
# installer is removed again in the layer that brought it; no download or
# build cache is kept. Packages and the standard library are compiled here:
# the container cannot write bytecode next to them, and compiling at every
# start would cost each command most of a second.
RUN --mount=type=bind,source=uv.lock,target=/src/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/src/pyproject.toml \
    --mount=type=bind,source=packages/core/pyproject.toml,target=/src/packages/core/pyproject.toml \
    --mount=type=bind,source=packages/runtime/pyproject.toml,target=/src/packages/runtime/pyproject.toml \
    --mount=type=bind,source=packages/toolkit/pyproject.toml,target=/src/packages/toolkit/pyproject.toml \
    pip install --no-cache-dir --root-user-action=ignore uv==0.9.18 \
    && cd /src \
    && UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
       uv sync --frozen --no-dev --no-install-workspace --no-cache --package dta-toolkit \
         --python /usr/local/bin/python3.12 \
    && pip uninstall --yes --root-user-action=ignore uv \
    && find /opt/venv/lib/python3.12/site-packages/pyarrow -type d -name tests -prune \
         -exec rm -rf {} + \
    && python -m compileall -q -j 0 /usr/local/lib/python3.12 \
         /opt/venv/lib/python3.12/site-packages \
    && rm -rf /root/.cache /tmp/* \
    && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 dta

RUN --mount=type=bind,source=.,target=/src,rw \
    pip install --no-cache-dir --root-user-action=ignore uv==0.9.18 \
    && cd /src \
    && UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
       uv sync --frozen --no-dev --no-editable --no-cache --package dta-toolkit \
         --python /usr/local/bin/python3.12 \
    && pip uninstall --yes --root-user-action=ignore uv \
    && python -m compileall -q -j 0 /opt/venv/lib/python3.12/site-packages/dta_* \
    && rm -rf /root/.cache /tmp/*

USER dta
WORKDIR /tmp

ENTRYPOINT ["dta"]
