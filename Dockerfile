# Stage 1: Fetch the verified upstream KeePassXC runtime and the browser
# extension. Debian's system-Qt build can crash when an automatically unlocked
# GUI is launched in the background; the official AppImage bundles its tested
# Qt runtime and does not exhibit that failure.
FROM python:3.12-slim AS keepassxc-assets

ARG TARGETARCH
ARG KEEPASSXC_VERSION=2.7.12
ARG KEEPASSXC_APPIMAGE_SHA256=564fe8b751b9ef7aa057e4d3d0b2878db24eaa0f6b1c855c82e699ab0913ae49
ARG KEEPASSXC_BROWSER_VERSION=1.9.7
ARG KEEPASSXC_FIREFOX_SHA256=f4ecad9cabe70511fbff42fe4fe831c560cc3a0b6da10a740ce670f4f6597f42
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
        -o Acquire::Retries=5 \
        -o Binary::apt::APT::Keep-Downloaded-Packages=true \
        install -y --no-install-recommends \
        ca-certificates curl \
    && cd /tmp \
    && apt-get download webext-keepassxc-browser \
    && mkdir /keepassxc-browser-package \
    && dpkg-deb -x webext-keepassxc-browser_*.deb /keepassxc-browser-package \
    && rm webext-keepassxc-browser_*.deb \
    && python -c \
        "import json; p='/keepassxc-browser-package/usr/share/chromium/extensions/keepassxc-browser/manifest.json'; assert json.load(open(p, encoding='utf-8'))['version'] == '${KEEPASSXC_BROWSER_VERSION}'"

# Keep the signed persistent addon intact for both Firefox and Camoufox.
RUN curl --fail --show-error --silent --location --retry 5 --retry-all-errors \
        https://addons.mozilla.org/firefox/downloads/file/4441759/keepassxc_browser-1.9.7.xpi \
        --output /keepassxc-browser-firefox.xpi \
    && echo "${KEEPASSXC_FIREFOX_SHA256}  /keepassxc-browser-firefox.xpi" | sha256sum -c -

RUN test "${TARGETARCH}" = "amd64" \
    || { echo "KeePassXC ${KEEPASSXC_VERSION} has no official Linux ${TARGETARCH} AppImage." >&2; exit 1; } \
    && curl --fail --show-error --silent --location \
        --connect-timeout 30 --max-time 300 \
        --retry 5 --retry-all-errors --retry-delay 2 \
        "https://github.com/keepassxreboot/keepassxc/releases/download/${KEEPASSXC_VERSION}/KeePassXC-${KEEPASSXC_VERSION}-x86_64.AppImage" \
        --output /tmp/KeePassXC.AppImage \
    && echo "${KEEPASSXC_APPIMAGE_SHA256}  /tmp/KeePassXC.AppImage" | sha256sum -c - \
    && chmod +x /tmp/KeePassXC.AppImage \
    && cd /opt \
    && /tmp/KeePassXC.AppImage --appimage-extract >/dev/null \
    && mv squashfs-root keepassxc \
    && rm /tmp/KeePassXC.AppImage

# Stage 2: Build React frontend
FROM node:20-slim AS frontend-builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 3: Production image
FROM python:3.12-slim

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security

# Chromium system deps
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 fonts-liberation \
    libgl1 libgl1-mesa-dri libegl-mesa0 \
    procps wget curl ca-certificates xclip xdotool libgpg-error0 libusb-1.0-0 \
    tzdata mesa-va-drivers mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*

# Install the checksum-pinned official runtime. Small stable wrappers preserve
# the expected executable paths for the manager and native messaging manifest.
COPY --from=keepassxc-assets /opt/keepassxc /opt/keepassxc
COPY docker/keepassxc docker/keepassxc-cli docker/keepassxc-proxy /usr/bin/
RUN chmod 755 /usr/bin/keepassxc /usr/bin/keepassxc-cli /usr/bin/keepassxc-proxy \
    && test "$(keepassxc-cli --version)" = "2.7.12"

# Bundle the unpacked Chromium extension at a stable path and install the
# native messaging host manifest for both Chromium and Chrome-derived builds.
# KeePassXC-Browser ships passkeys disabled by default; change only the initial
# default so users can still turn the feature off in extension settings.
COPY docker/keepassxc-native-messaging.json /tmp/keepassxc-native-messaging.json
COPY --from=keepassxc-assets /keepassxc-browser-package/usr/share/chromium/extensions/keepassxc-browser \
    /opt/cloakbrowser/extensions/keepassxc-browser
COPY --from=keepassxc-assets /keepassxc-browser-firefox.xpi /opt/cloakbrowser/extensions/keepassxc-browser-firefox.xpi
COPY docker/keepassxc-firefox-native-messaging.json /usr/lib/mozilla/native-messaging-hosts/org.keepassxc.keepassxc_browser.json
RUN mkdir -p /opt/cloakbrowser/extensions \
    /etc/chromium/native-messaging-hosts \
    /etc/opt/chrome/native-messaging-hosts \
    && sed -i 's/passkeys: false,/passkeys: true,/' \
        /opt/cloakbrowser/extensions/keepassxc-browser/background/page.js \
    && grep -q 'passkeys: true,' \
        /opt/cloakbrowser/extensions/keepassxc-browser/background/page.js \
    && cp /tmp/keepassxc-native-messaging.json \
        /etc/chromium/native-messaging-hosts/org.keepassxc.keepassxc_browser.json \
    && cp /tmp/keepassxc-native-messaging.json \
        /etc/opt/chrome/native-messaging-hosts/org.keepassxc.keepassxc_browser.json \
    && rm /tmp/keepassxc-native-messaging.json

# Playwright system deps (matches test-infra)
RUN pip install --no-cache-dir playwright && playwright install-deps chromium 2>/dev/null || true && pip uninstall -y playwright

# Windows core fonts (Arial, Times New Roman, Verdana, etc.)
RUN sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && apt-get -o Acquire::Retries=5 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends ttf-mscorefonts-installer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Install the amd64 KasmVNC package matching the supported image architecture.
ARG TARGETARCH
RUN wget -q https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb \
    && apt-get update && apt-get install -y -f ./kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb \
    && rm kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Browser pins live in every release commit. Never resolve latest in a build.
COPY docker/browser-versions.json docker/install_browsers.py /opt/cbm/
RUN python /opt/cbm/install_browsers.py native \
    && rm -rf /var/lib/apt/lists/*

# Pre-download CloakBrowser binary. This only depends on the installed
# cloakbrowser package, so keep it before application code copies.
RUN --mount=type=cache,target=/root/.cloakbrowser,sharing=locked \
    python /opt/cbm/install_browsers.py cloakbrowser

# Download Camoufox and its default addon/GeoIP assets during the build, so
# launching a profile never needs to download a browser binary at runtime.
RUN --mount=type=cache,target=/var/cache/cbm-browsers,sharing=locked \
    python /opt/cbm/install_browsers.py camoufox \
    && python /opt/cbm/install_browsers.py verify

ENV CLOAKBROWSER_BINARY_PATH=/opt/cbm/cloakbrowser/chrome \
    CLOAKBROWSER_AUTO_UPDATE=false

# Backend code
COPY backend/ /app/backend/

# Frontend build from stage 1
COPY --from=frontend-builder /build/dist /app/frontend/dist

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

VOLUME /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

ARG GIT_COMMIT=unknown
ARG RELEASE_VERSION=development
LABEL org.opencontainers.image.revision=$GIT_COMMIT \
    org.opencontainers.image.version=$RELEASE_VERSION
