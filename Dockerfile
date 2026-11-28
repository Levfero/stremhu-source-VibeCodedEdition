# ---- Node.js Build Stage ----
FROM node:24-bookworm-slim AS node-build

WORKDIR /app
ARG APP_VERSION=0.0.0

# Build Client
WORKDIR /app/client
COPY client/package.json client/package-lock.json ./
RUN npm ci
COPY client ./
RUN npm run build

# ---- wireproxy (userspace WireGuard a torrent-VPN-hez, VPN-terv Rev. 5, 8. fejezet) ----
FROM python:3.12-slim-bookworm AS wireproxy

ARG TARGETARCH
ARG WIREPROXY_VERSION=v1.1.3
# A kiadás checksums.txt-je szerinti archívum-hash-ek; eltérésnél a build megáll.
ARG WIREPROXY_SHA256_AMD64=e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c
ARG WIREPROXY_SHA256_ARM64=370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6
# A PoC-ban mért amd64-bináris hash-e (harmadik, független ellenőrzés).
ARG WIREPROXY_BINARY_SHA256_AMD64=70ae5e52223dac7974af8d98a321f14a0e1689d2b14655ebc8dadfa1ec69466d

COPY docker/fetch_wireproxy.py /tmp/fetch_wireproxy.py
RUN ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
  case "${ARCH}" in \
    amd64) python /tmp/fetch_wireproxy.py "${WIREPROXY_VERSION}" amd64 \
      "${WIREPROXY_SHA256_AMD64}" "${WIREPROXY_BINARY_SHA256_AMD64}" ;; \
    arm64) python /tmp/fetch_wireproxy.py "${WIREPROXY_VERSION}" arm64 \
      "${WIREPROXY_SHA256_ARM64}" - ;; \
    *) echo "wireproxy: unsupported architecture ${ARCH}" >&2; exit 1 ;; \
  esac

# ---- Runtime Stage ----
FROM python:3.12-slim-bookworm AS runtime

ARG APP_VERSION=0.0.0

RUN apt-get update && apt-get upgrade -y && \
  apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
  && rm -rf /var/lib/apt/lists/*

# Force glibc to prefer IPv4 over IPv6 (RFC 3484 precedence) to avoid reCAPTCHA issues
RUN echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf

WORKDIR /app

COPY server/requirements.txt ./

RUN python -m venv /opt/venv && \
  /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --from=node-build /app/client/dist ./client
COPY --from=wireproxy /out/wireproxy /usr/local/bin/wireproxy

COPY server ./
ENV NODE_ENV=prod
ENV VERSION=${APP_VERSION}
ENV PATH="/opt/venv/bin:${PATH}"
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_DIR=/etc/ssl/certs
# glibc-allokátor: a dinamikus mmap/trim küszöb a glibc saját maximumán (32 MiB / 2× = 64 MiB)
# rögzítve. Enélkül egy letöltött, azonnal felszabadított torrenttörzs memóriáját a glibc
# minden kérés végén visszaadja az OS-nek, és a következő letöltés friss lapokon fut
# (cr15a-after-v5 FAIL, cr15a-diag7/diag8).
ENV MALLOC_MMAP_THRESHOLD_=33554432
ENV MALLOC_TRIM_THRESHOLD_=67108864

EXPOSE 7070/tcp
EXPOSE 6881/tcp 6881/udp

CMD ["python", "-m", "app.run"]
