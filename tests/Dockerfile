# Runs this repository's test suite in Linux.
#
# Home Assistant's test harness cannot be installed on Windows without a
# C toolchain — `lru-dict` builds from source and fails with "Unable to
# find a compatible Visual Studio installation". Rather than ask anyone
# to install build tools, the suite runs in a container that matches what
# CI uses.
#
# Built from the repository root so it reads the one real
# requirements_test.txt rather than a second copy that would drift:
#
#   docker build -t ha-pytest -f tests/Dockerfile .
#   docker run --rm -v "$(pwd):/repo" ha-pytest python -m pytest
#
# See tests/e2e/README.md for the end-to-end suite, which additionally
# needs the containers `up.sh` starts and has to join their network.
FROM python:3.13-slim

# gcc/python3-dev are for the handful of harness dependencies that ship
# no wheel; nothing in this repository needs compiling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_test.txt /tmp/requirements_test.txt
RUN pip install --no-cache-dir -r /tmp/requirements_test.txt

WORKDIR /repo
