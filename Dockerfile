# syntax=docker/dockerfile:1.4
# https://github.com/moby/buildkit/blob/master/frontend/dockerfile/docs/reference.md

# Stage: compile-frontend
# Purpose: Compiles the frontend
# Notes:
#  - Does NPM stuff with Typescript and such
FROM --platform=$BUILDPLATFORM node:16-bullseye-slim AS compile-frontend

COPY ./src-ui /src/src-ui

WORKDIR /src/src-ui
RUN set -eux \
  && npm update npm -g \
  && npm ci --omit=optional
RUN set -eux \
  && ./node_modules/.bin/ng build --configuration production

# Stage: pipenv-base
# Purpose: Generates a requirements.txt file for building
# Comments:
#  - pipenv dependencies are not left in the final image
#  - pipenv can't touch the final image somehow
FROM --platform=$BUILDPLATFORM python:3.9-slim-bullseye as pipenv-base

WORKDIR /usr/src/pipenv

COPY Pipfile* ./

RUN set -eux \
  && echo "Installing pipenv" \
    && python3 -m pip install --no-cache-dir --upgrade pipenv==2023.2.18 \
  && echo "Generating requirement.txt" \
    && pipenv requirements > requirements.txt

# Stage: s6-overlay-base
# Purpose: Installs s6-overlay and rootfs
# Comments:
#  - Don't leave anything extra in here
FROM python:3.9-slim-bullseye as s6-overlay-base

WORKDIR /usr/src/s6

ENV \
    LANG="C.UTF-8" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PREFER_BINARY=1 \
    PS1="$(whoami)@$(hostname):$(pwd)$ " \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_CMD_WAIT_FOR_SERVICES=1

# Buildx provided, must be defined to use though
ARG TARGETARCH
ARG TARGETVARIANT
ARG S6_OVERLAY_VERSION=3.1.4.1

ARG S6_BUILD_TIME_PKGS="curl \
                        xz-utils"

RUN set -eux \
    && echo "Installing build time packages" \
      && apt-get update \
      && apt-get install --yes --quiet --no-install-recommends ${S6_BUILD_TIME_PKGS} \
    && echo "Determining arch" \
      && S6_ARCH="" \
      && if [ "${TARGETARCH}${TARGETVARIANT}" = "amd64" ]; then S6_ARCH="x86_64"; \
      elif [ "${TARGETARCH}${TARGETVARIANT}" = "aarch64" ]; then S6_ARCH="aarch64"; \
      elif [ "${TARGETARCH}${TARGETVARIANT}" = "armv7" ]; then S6_ARCH="armhf"; fi \
    && echo "Installing s6-overlay for ${S6_ARCH}" \
      && curl --fail --silent --show-error -L --output s6-overlay-noarch.tar.xz --location "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
      && curl --fail --silent --show-error -L --output s6-overlay-noarch.tar.xz.sha256 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz.sha256" \
      && curl --fail --silent --show-error -L --output s6-overlay-${S6_ARCH}.tar.xz --location "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
      && curl --fail --silent --show-error -L --output s6-overlay-${S6_ARCH}.tar.xz.sha256 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz.sha256" \
      && curl --fail --silent --show-error -L --output s6-overlay-symlinks-noarch.tar.xz "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-symlinks-noarch.tar.xz" \
      && curl --fail --silent --show-error -L --output s6-overlay-symlinks-noarch.tar.xz.sha256 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-symlinks-noarch.tar.xz.sha256" \
      && curl --fail --silent --show-error -L --output s6-overlay-symlinks-arch.tar.xz "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-symlinks-arch.tar.xz" \
      && curl --fail --silent --show-error -L --output s6-overlay-symlinks-arch.tar.xz.sha256 "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-symlinks-arch.tar.xz.sha256" \
      && echo "Validating s6-archives" \
        && sha256sum -c ./*.sha256 \
      && echo "Unpacking archives" \
        && tar -C / -Jxpf s6-overlay-noarch.tar.xz \
        && tar -C / -Jxpf s6-overlay-${S6_ARCH}.tar.xz \
        && tar -C / -Jxpf s6-overlay-symlinks-noarch.tar.xz \
        && tar -C / -Jxpf s6-overlay-symlinks-arch.tar.xz \
      && echo "Removing downloaded archives" \
        && rm ./*.tar.xz \
        && rm ./*.sha256 \
    && echo "Installing bashio" \
      && curl --fail --silent -J -L --output /tmp/bashio.tar.gz "https://github.com/hassio-addons/bashio/archive/v0.14.3.tar.gz" \
      && mkdir /tmp/bashio \
      && tar zxvf /tmp/bashio.tar.gz --strip 1 -C /tmp/bashio \
      && mv /tmp/bashio/lib /usr/lib/bashio \
      && ln -s /usr/lib/bashio/bashio /usr/bin/bashio \
      && rm /tmp/bashio.tar.gz \
    && echo "Cleaning up image" \
      && apt-get -y purge ${S6_BUILD_TIME_PKGS} \
      && apt-get -y autoremove --purge \
      && rm -rf /var/lib/apt/lists/*

# Stage: main-app
# Purpose: The final image
# Comments:
#  - Don't leave anything extra in here
FROM s6-overlay-base as main-app

LABEL org.opencontainers.image.authors="paperless-ngx team <hello@paperless-ngx.com>"
LABEL org.opencontainers.image.documentation="https://docs.paperless-ngx.com/"
LABEL org.opencontainers.image.source="https://github.com/paperless-ngx/paperless-ngx"
LABEL org.opencontainers.image.url="https://github.com/paperless-ngx/paperless-ngx"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

ARG DEBIAN_FRONTEND=noninteractive
# Buildx provided, must be defined to use though
ARG TARGETARCH
ARG TARGETVARIANT

# Workflow provided
ARG JBIG2ENC_VERSION
ARG QPDF_VERSION
ARG PIKEPDF_VERSION
ARG PSYCOPG2_VERSION

#
# Begin installation and configuration
# Order the steps below from least often changed to most
#

# Packages need for running
ARG RUNTIME_PACKAGES="\
  # General utils
  curl \
  # Docker specific
  gosu \
  # Timezones support
  tzdata \
  # fonts for text file thumbnail generation
  fonts-liberation \
  gettext \
  ghostscript \
  gnupg \
  icc-profiles-free \
  imagemagick \
  # Image processing
  liblept5 \
  liblcms2-2 \
  libtiff5 \
  libfreetype6 \
  libwebp6 \
  libopenjp2-7 \
  libimagequant0 \
  libraqm0 \
  libjpeg62-turbo \
  # PostgreSQL
  libpq5 \
  postgresql-client \
  # MySQL / MariaDB
  mariadb-client \
  # For Numpy
  libatlas3-base \
  # OCRmyPDF dependencies
  tesseract-ocr \
  tesseract-ocr-eng \
  tesseract-ocr-deu \
  tesseract-ocr-fra \
  tesseract-ocr-ita \
  tesseract-ocr-spa \
  unpaper \
  pngquant \
  # pikepdf / qpdf
  jbig2dec \
  libxml2 \
  libxslt1.1 \
  libgnutls30 \
  # Mime type detection
  file \
  libmagic1 \
  media-types \
  zlib1g \
  # Barcode splitter
  libzbar0 \
  poppler-utils \
  # RapidFuzz on armv7
  libatomic1"

# Install basic runtime packages.
# These change very infrequently
RUN set -eux \
  echo "Installing system packages" \
    && apt-get update \
    && apt-get install --yes --quiet --no-install-recommends ${RUNTIME_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

# Copy gunicorn config
# Changes very infrequently
WORKDIR /usr/src/paperless/

COPY gunicorn.conf.py .

# setup docker-specific things
# These change sometimes, but rarely
WORKDIR /usr/src/paperless/src/docker/

COPY [ \
  "docker/imagemagick-policy.xml", \
  "docker/wait-for-redis.py", \
  "docker/management_script.sh", \
  "docker/install_management_commands.sh", \
  "/usr/src/paperless/src/docker/" \
]

RUN set -eux \
  && echo "Configuring ImageMagick" \
    && mv imagemagick-policy.xml /etc/ImageMagick-6/policy.xml \
  && echo "Setting up Docker scripts" \
    && mv wait-for-redis.py /sbin/wait-for-redis.py \
    && chmod 755 /sbin/wait-for-redis.py \
  && echo "Installing managment commands" \
    && chmod +x install_management_commands.sh \
    && ./install_management_commands.sh

# Install the built packages from the installer library images
# These change sometimes
RUN set -eux \
  && echo "Getting binaries" \
    && mkdir paperless-ngx \
    && curl --fail --silent --show-error --output paperless-ngx.tar.gz --location https://github.com/paperless-ngx/paperless-ngx/archive/2a18975fe33e7b1c06254a72a2433c914cd49ed1.tar.gz \
    && tar -xf paperless-ngx.tar.gz --directory paperless-ngx --strip-components=1 \
    && cd paperless-ngx \
    # Setting a specific revision ensures we know what this installed
    # and ensures cache breaking on changes
  && echo "Installing jbig2enc" \
    && cp ./jbig2enc/${JBIG2ENC_VERSION}/${TARGETARCH}${TARGETVARIANT}/jbig2 /usr/local/bin/ \
    && cp ./jbig2enc/${JBIG2ENC_VERSION}/${TARGETARCH}${TARGETVARIANT}/libjbig2enc* /usr/local/lib/ \
  && echo "Installing qpdf" \
    && apt-get install --yes --no-install-recommends ./qpdf/${QPDF_VERSION}/${TARGETARCH}${TARGETVARIANT}/libqpdf29_*.deb \
    && apt-get install --yes --no-install-recommends ./qpdf/${QPDF_VERSION}/${TARGETARCH}${TARGETVARIANT}/qpdf_*.deb \
  && echo "Installing pikepdf and dependencies" \
    && python3 -m pip install --no-cache-dir ./pikepdf/${PIKEPDF_VERSION}/${TARGETARCH}${TARGETVARIANT}/*.whl \
    && python3 -m pip list \
  && echo "Installing psycopg2" \
    && python3 -m pip install --no-cache-dir ./psycopg2/${PSYCOPG2_VERSION}/${TARGETARCH}${TARGETVARIANT}/psycopg2*.whl \
    && python3 -m pip list \
  && echo "Cleaning up image layer" \
    && cd ../ \
    && rm -rf paperless-ngx \
    && rm paperless-ngx.tar.gz

WORKDIR /usr/src/paperless/src/

# Python dependencies
# Change pretty frequently
COPY --from=pipenv-base /usr/src/pipenv/requirements.txt ./

# Packages needed only for building a few quick Python
# dependencies
ARG BUILD_PACKAGES="\
  build-essential \
  git \
  default-libmysqlclient-dev \
  python3-dev"

RUN set -eux \
  && echo "Installing build system packages" \
    && apt-get update \
    && apt-get install --yes --quiet --no-install-recommends ${BUILD_PACKAGES} \
    && python3 -m pip install --no-cache-dir --upgrade wheel \
  && echo "Installing Python requirements" \
    && python3 -m pip install --default-timeout=1000 --no-cache-dir --requirement requirements.txt \
  && echo "Installing NLTK data" \
    && python3 -W ignore::RuntimeWarning -m nltk.downloader -d "/usr/share/nltk_data" snowball_data \
    && python3 -W ignore::RuntimeWarning -m nltk.downloader -d "/usr/share/nltk_data" stopwords \
    && python3 -W ignore::RuntimeWarning -m nltk.downloader -d "/usr/share/nltk_data" punkt \
  && echo "Cleaning up image" \
    && apt-get -y purge ${BUILD_PACKAGES} \
    && apt-get -y autoremove --purge \
    && apt-get clean --yes \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /var/tmp/* \
    && rm -rf /var/cache/apt/archives/* \
    && truncate -s 0 /var/log/*log

# copy backend
COPY ./src ./

# copy frontend
COPY --from=compile-frontend /src/src/documents/static/frontend/ ./documents/static/frontend/

# add users, setup scripts
# Mount the compiled frontend to expected location
RUN set -eux \
  && addgroup --gid 1000 paperless \
  && useradd --uid 1000 --gid paperless --home-dir /usr/src/paperless paperless \
  && chown -R paperless:paperless /usr/src/paperless \
  && gosu paperless python3 manage.py collectstatic --clear --no-input --link \
  && gosu paperless python3 manage.py compilemessages

VOLUME ["/usr/src/paperless/data", \
        "/usr/src/paperless/media", \
        "/usr/src/paperless/consume", \
        "/usr/src/paperless/export"]

ENTRYPOINT ["/init"]

EXPOSE 8000

# TODO Move this to the s6-base.  Here for caching
COPY ./docker/rootfs /
