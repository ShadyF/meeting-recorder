FROM ubuntu:24.04

# Keep the baseline limited to Python checks and ordinary source-control work.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash developer

USER developer
