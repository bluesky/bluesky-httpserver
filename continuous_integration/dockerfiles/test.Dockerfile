ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    git \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt requirements-dev.txt ./
COPY pyproject.toml setup.py setup.cfg MANIFEST.in versioneer.py README.rst AUTHORS.rst LICENSE ./
COPY bluesky_httpserver ./bluesky_httpserver

RUN python -m pip install --upgrade pip setuptools wheel numpy && \
    python -m pip install git+https://github.com/bluesky/bluesky-queueserver.git && \
    python -m pip install git+https://github.com/bluesky/bluesky-queueserver-api.git && \
    python -m pip install -r requirements-dev.txt && \
    python -m pip install .

COPY scripts/docker/run_shard_in_container.sh /usr/local/bin/run_shard_in_container.sh
RUN chmod +x /usr/local/bin/run_shard_in_container.sh

ENTRYPOINT ["/usr/local/bin/run_shard_in_container.sh"]
