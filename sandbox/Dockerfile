# Base image for the student sandbox and the evaluation container.
# Runs with --network none, a read-only root filesystem and a non-root user (B11).
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep patch \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest==9.1.1" \
    && groupadd --gid 1000 student \
    && useradd --uid 1000 --gid student --home-dir /home/student --create-home --shell /bin/bash student \
    && mkdir -p /work \
    && chown student:student /work

USER student
WORKDIR /work
ENV HOME=/home/student

CMD ["sleep", "infinity"]
