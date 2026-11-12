# Light Bee: Bluestaq App Store, python template.
#
# Contract: the server reads PORT (default 8080) and binds HOST, set below to
# 0.0.0.0; `/`, `/healthz`, `/readyz`, `/livez`, `/ping` answer 200
# unauthenticated. There is deliberately no ENV PORT: the platform injects it,
# and a baked value can win over the injected one.
#
# Alpine, not Debian slim (1.0.0, 11 November 2026). A local grype scan of the
# Debian trixie build found 59 high findings in base packages that Debian marks
# "won't fix" (glibc, util-linux, ncurses, perl, the gcc runtime, sqlite), so
# no upgrade clears them and Container Scan requires every critical and high
# resolved. DRIFTER failed Container Scan on the same base on 3 November 2026
# and passed on Alpine. Python stays 3.12: the 3.12.16 interpreter has no
# critical or high finding.
# Evidence: docs/evidence/container-scan-1.0.0.md.
#
# Shape:
#   build  installs the hash-locked runtime closure into a venv, wheels only
#          (the lock lists every published hash, musllinux wheels included)
#   prep   patches the OS, drops SQLite (unused, and its open findings have no
#          fix), deletes every copy of pip and the apk binary, creates the
#          user, strips setuid and setgid bits as the LAST mutation
#   final  FROM scratch with one COPY of prep, so the image-policy scan sees a
#          single clean layer and no history
#
# The base tag floats on purpose: Container Scan runs on every upload, so each
# build should pick up the latest patched base. A digest pin would rot silently.

FROM python:3.12-alpine AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-runtime.txt .
# Wheels only (no setup.py runs at build time) and every file hash-checked.
# The base image's own pip does the install; it is deleted from the image below.
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r requirements-runtime.txt

FROM python:3.12-alpine AS prep
# Fail closed: an unpatched image is worse than a failed build.
RUN apk upgrade --no-cache
# SQLite has no use in Light Bee (nothing imports sqlite3) and its open high
# findings have no fixed build. The base pins it through its .python-rundeps
# virtual package, so swap in our own virtual with the same shared-library
# list minus libsqlite3, then remove the library and the module that loads it.
# apk del exits 0 when another package still needs sqlite-libs, so the last
# two commands check the result: the build fails if any of it survives.
RUN apk info -R .python-rundeps > /tmp/rundeps \
 && grep '^so:' /tmp/rundeps > /tmp/so \
 && grep -v '^so:libsqlite3' /tmp/so > /tmp/keep \
 && xargs apk add --no-cache --virtual .lightbee-rundeps < /tmp/keep \
 && apk del --no-cache .python-rundeps sqlite-libs \
 && rm -rf /tmp/rundeps /tmp/so /tmp/keep \
           /usr/local/lib/python3.12/sqlite3 \
           /usr/local/lib/python3.12/lib-dynload/_sqlite3.cpython-312-*.so \
 && ! apk info -e sqlite-libs \
 && [ -z "$(find / -xdev \( -name 'libsqlite3.so*' -o -name '_sqlite3.*' \
        -o -path /usr/local/lib/python3.12/sqlite3 \) -print)" ]
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
# The package manager has no runtime purpose. Its binary and configuration go;
# the installed database (/lib/apk/db) stays, so the image scanner can still
# inventory the OS packages honestly.
RUN rm -f /sbin/apk /var/log/apk.log \
 && rm -rf /etc/apk /var/cache/apk
# User first, then the setuid and setgid sweep LAST, so nothing re-adds a bit.
# The either-bit test is two -perm clauses, which every find accepts (POSIX
# find has no `-perm /mode`; BusyBox and GNU find do).
# Nothing is chowned: the app writes nothing to disk.
RUN addgroup -g 10001 appuser \
 && adduser -D -H -u 10001 -G appuser -s /sbin/nologin appuser \
 && find / -xdev \( -perm -4000 -o -perm -2000 \) \( -type f -o -type d \) -exec chmod a-s {} +

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
