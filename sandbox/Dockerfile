# Base image for the student sandbox and the evaluation container.
# Runs with --network none, a read-only root filesystem and a non-root user (B11).
FROM python:3.15-slim-bookworm@sha256:2a41267d9de9d358f77a1de356e0f16ecba8d39827855be0ce9ef4792edce402

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep patch shellcheck \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest==9.1.1" "ruff==0.17.1" \
    && groupadd --gid 1000 student \
    && useradd --uid 1000 --gid student --home-dir /home/student --create-home --shell /bin/bash student \
    && mkdir -p /work \
    && chown student:student /work

USER student
WORKDIR /work
ENV HOME=/home/student

CMD ["sleep", "infinity"]
