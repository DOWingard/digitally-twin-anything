# The twin's own services: one image, one command per service.
#
#   dta-runtime api       the HTTP API (port 8000)
#   dta-runtime ui        the browser interface (port 8050)
#   dta-runtime install   create the twin or bring it in line with a manifest (one-off)
#   dta-runtime load      read every source once (one-off)
#
# Build with the repository root as context:
#
#   docker build -f docker/runtime.Dockerfile -t dta-runtime:0.1.0 .
#
# What the build may read is limited by docker/runtime.Dockerfile.dockerignore.
# Every setting, and every secret, reaches a container through its environment;
# nothing is baked into the image.

FROM python:3.12-slim

# dlt reports usage unless told not to; a twin never reports anything.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUNTIME__DLTHUB_TELEMETRY=false \
    PATH=/opt/venv/bin:$PATH

# One layer holds everything. The sources are mounted for the build, not
# copied; the installer is removed again; no download or build cache is kept.
# The packages are compiled once here: a container cannot write bytecode next
# to them, and compiling at every start costs each process seconds.
RUN --mount=type=bind,source=.,target=/src,rw \
    pip install --no-cache-dir --root-user-action=ignore uv==0.9.18 \
    && cd /src \
    && UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
       uv sync --frozen --no-dev --no-editable --no-cache --package dta-runtime \
         --python /usr/local/bin/python3.12 \
    && pip uninstall --yes --root-user-action=ignore uv \
    && for package in numpy pandas pyarrow; do \
         find /opt/venv/lib/python3.12/site-packages/$package -type d -name tests -prune \
           -exec rm -rf {} + ; \
       done \
    && python -m compileall -q -j 0 /opt/venv/lib/python3.12/site-packages \
    && rm -rf /root/.cache /tmp/* \
    && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 twin \
    && mkdir -p /var/lib/dta/artifacts /var/log/dta \
    && chown -R twin:twin /var/lib/dta /var/log/dta

USER twin
WORKDIR /var/lib/dta

# Container-side ports of the two long-running services.
EXPOSE 8000 8050

CMD ["dta-runtime", "api"]
