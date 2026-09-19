# Unbound behavior test harness.
#
# Builds Unbound 1.26.0 from source (release tarball, pinned checksum) so the
# behaviour under test is the behaviour of a known commit, not of whatever the
# distribution happens to ship.
#
#   unbound: 1.26.0 (release-1.26.0, a45da353d3feb5d8fc00685fa1ceda3816d5108f)
#
# The image deliberately does NOT need `ss`, and no test in it queries the
# outside world: forward-zone cases are observed by a fake upstream that the
# runner itself listens on (see runner/upstream.py).

FROM debian:bookworm-slim AS unbound-build

ARG UNBOUND_VERSION=1.26.0
ARG UNBOUND_SHA256=77458a7156e275c0b7b17fabcb357cb12445d95cfcb26fb9bb7d5ecba45e0b63

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gcc make libc6-dev \
    libssl-dev libexpat1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSLO "https://nlnetlabs.nl/downloads/unbound/unbound-${UNBOUND_VERSION}.tar.gz" \
    && echo "${UNBOUND_SHA256}  unbound-${UNBOUND_VERSION}.tar.gz" | sha256sum -c - \
    && tar xzf "unbound-${UNBOUND_VERSION}.tar.gz"

# No --with-libevent, no ipset, no dnstap: a plain default-ish build.
# Recorded here because the accepted `local-zone` type set depends on it:
# `ipset` is accepted by the config parser only as a string, and without
# USE_IPSET it fails later in local_zones_apply_cfg().
RUN cd "unbound-${UNBOUND_VERSION}" \
    && ./configure \
    --prefix=/usr/local \
    --with-conf-file=/etc/unbound/unbound.conf \
    --with-run-dir=/var/lib/unbound \
    --with-pidfile=/var/run/unbound.pid \
    --with-username= \
    && make -j"$(nproc)" \
    && make install


# Bookworm based, so the OpenSSL and expat the unbound binary was linked
# against in the build stage are the same ones here; and it carries the
# Python that pyproject.toml's requires-python asks for, which Debian's own
# python3 package (3.11 on bookworm) does not.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

# git is only for gen.py: it stamps the cases' commit into the generated
# files' header. Without it the header degrades to "cases: unknown", which
# defeats the point of recording which commit produced the tables.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates dnsutils git libssl3 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=unbound-build /usr/local/sbin/unbound /usr/local/sbin/
COPY --from=unbound-build /usr/local/sbin/unbound-checkconf /usr/local/sbin/
COPY --from=unbound-build /usr/local/sbin/unbound-control /usr/local/sbin/
COPY --from=unbound-build /usr/local/sbin/unbound-control-setup /usr/local/sbin/
COPY --from=unbound-build /usr/local/sbin/unbound-anchor /usr/local/sbin/
COPY --from=unbound-build /usr/local/lib/libunbound.so.9* /usr/local/lib/
RUN ldconfig && mkdir -p /etc/unbound /var/lib/unbound

ENV PATH="/usr/local/sbin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /work

# Dependencies first, so editing cases/ does not re-resolve them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY . .
ENV PATH="/opt/venv/bin:${PATH}"

CMD ["pytest", "-q"]
