# Light Bee: Bluestaq App Store, python template.
#
# Contract: the server reads PORT (default 8080) and binds HOST, set below to
# 0.0.0.0; `/`, `/healthz`, `/readyz`, `/livez`, `/ping` answer 200
# unauthenticated. There is deliberately no ENV PORT: the platform injects it,
# and a baked value can win over the injected one.
#
# Shape, carried from the predecessor's passes through all ten stages:
#   build  installs the hash-locked runtime closure into a venv, wheels only
#   prep   patches the OS, deletes every copy of pip, creates the user, strips
#          setuid and setgid bits as the LAST mutation
#   final  FROM scratch with one COPY of prep, so the image-policy scan sees a
#          single clean layer and no history
#
# The base tag floats on purpose: Container Scan runs on every upload, so each
# build should pick up the latest patched base. A digest pin would rot silently.

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-runtime.txt .
# Wheels only (no setup.py runs at build time) and every file hash-checked.
# The base image's own pip does the install; it is deleted from the image below.
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements-runtime.txt

FROM python:3.12-slim AS prep
# Fail closed: an unpatched image is worse than a failed build. The
# predecessor ran this same fail-closed upgrade through the platform's
# Container Build stage successfully (3 November 2026).
RUN apt-get update \
 && apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY src ./src
# pip has no runtime purpose and the scanners read every copy of it.
RUN rm -rf /opt/venv/lib/python3.12/site-packages/pip \
           /opt/venv/lib/python3.12/site-packages/pip-*.dist-info \
           /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.12 \
           /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
           /usr/local/lib/python3.12/ensurepip
# The package manager has no runtime purpose. Its binaries go; the dpkg
# status database stays, so the image scanner can still inventory the OS
# packages honestly.
RUN rm -f /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config \
          /usr/bin/apt-mark /usr/bin/dpkg /usr/bin/dpkg-* \
 && rm -rf /etc/apt /var/cache/apt /var/lib/apt
# User first, then the setuid and setgid sweep LAST, so nothing re-adds a bit.
# Nothing is chowned: the app writes nothing to disk.
RUN groupadd -g 10001 appuser \
 && useradd -u 10001 -g 10001 -r -M -s /usr/sbin/nologin appuser \
 && find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} +

FROM scratch
# A considered flatten, not an accidental whole-stage copy: collapsing history
# is what keeps a removed setuid bit out of the scanned layers.
# hadolint ignore=DL3067
COPY --from=prep / /
ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0
WORKDIR /app
USER 10001:10001
EXPOSE 8080
# Exec form: no shell, and SIGTERM reaches the server. One worker, by design.
CMD ["python", "-m", "lightbee"]
