# The twin's own services: one image, one command per service.
#
#   dta-runtime api       the HTTP API (port 8000)
#   dta-runtime ui        the browser interface (port 8050)
#   dta-runtime install   create the twin or bring it in line with a manifest (one-off)
#   dta-runtime load      read every source once (one-off)
#
# The plugin's launcher builds it the first time it is used, from the plugin's
# root (the repository's root) as context; by hand:
#
#   docker build -f docker/runtime.Dockerfile -t dta-runtime:0.1.0 .
#
# What the build may read is limited by docker/runtime.Dockerfile.dockerignore;
# it needs only committed files. What it pulls: the base image, by digest; the
# third-party packages, by the lock file's versions and hashes; the installer,
# by version; the build backend of the project's own packages (hatchling) at
# its newest version, the one thing not pinned. Every setting, and every
# secret, reaches a container through its environment; nothing is baked into
# the image.

# The same base as the toolkit's image, by digest, so that a build next year
# starts from the same files.
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# dlt reports usage unless told not to; a twin never reports anything.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUNTIME__DLTHUB_TELEMETRY=false \
    PATH=/opt/venv/bin:$PATH

# Two layers. The first holds the third-party packages and is made from the
# lock file and the packages' metadata alone, so it is built once per lock
# file and shared by every image built after it; the second holds this
# project's own packages and is all that a change to their code rebuilds.
# Nothing is copied for the build, only mounted; the installer is removed
# again in the layer that brought it; no download or build cache is kept.
# Packages and the standard library are compiled here: a container cannot
# write bytecode next to them, and compiling at every start costs each
# process most of a second (a run of a twin's own code is a new process).
RUN --mount=type=bind,source=uv.lock,target=/src/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/src/pyproject.toml \
    --mount=type=bind,source=packages/core/pyproject.toml,target=/src/packages/core/pyproject.toml \
    --mount=type=bind,source=packages/runtime/pyproject.toml,target=/src/packages/runtime/pyproject.toml \
    --mount=type=bind,source=packages/toolkit/pyproject.toml,target=/src/packages/toolkit/pyproject.toml \
    pip install --no-cache-dir --root-user-action=ignore uv==0.9.18 \
    && cd /src \
    && UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
       uv sync --frozen --no-dev --no-install-workspace --no-cache --package dta-runtime \
         --python /usr/local/bin/python3.12 \
    && pip uninstall --yes --root-user-action=ignore uv \
    && for package in numpy pandas pyarrow; do \
         find /opt/venv/lib/python3.12/site-packages/$package -type d -name tests -prune \
           -exec rm -rf {} + ; \
       done \
    && python -m compileall -q -j 0 /usr/local/lib/python3.12 \
         /opt/venv/lib/python3.12/site-packages \
    && rm -rf /root/.cache /tmp/* \
    && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 twin \
    && mkdir -p /var/lib/dta/artifacts /var/log/dta \
    && chown -R twin:twin /var/lib/dta /var/log/dta

RUN --mount=type=bind,source=.,target=/src,rw \
    pip install --no-cache-dir --root-user-action=ignore uv==0.9.18 \
    && cd /src \
    && UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
       uv sync --frozen --no-dev --no-editable --no-cache --package dta-runtime \
         --python /usr/local/bin/python3.12 \
    && pip uninstall --yes --root-user-action=ignore uv \
    && python -m compileall -q -j 0 /opt/venv/lib/python3.12/site-packages/dta_* \
    && rm -rf /root/.cache /tmp/*

USER twin
WORKDIR /var/lib/dta

# Container-side ports of the two long-running services.
EXPOSE 8000 8050

CMD ["dta-runtime", "api"]
