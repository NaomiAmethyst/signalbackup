# syntax=docker/dockerfile:1
FROM python:3.13-alpine AS build
WORKDIR /src
RUN apk add --no-cache binutils gcc musl-dev zlib-dev
COPY tools/requirements-build.txt tools/requirements-build.txt
RUN python -m pip install --no-cache-dir -r tools/requirements-build.txt
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY signalbackup/ signalbackup/
COPY tools/entrypoint.py tools/build_binary.py tools/
RUN python -m pip install --no-cache-dir . \
    && python tools/build_binary.py --onedir

# PyInstaller bundles Python and its extension libraries, but deliberately
# excludes libc. Alpine's musl loader is also its libc. The bootloader needs
# zlib before it can load any of the bundled libraries.
FROM scratch
COPY --from=build /lib/ld-musl-*.so.1 /usr/lib/libz.so.1* /lib/
COPY --from=build /src/dist/sigbackup/ /app/
COPY --from=build /src/LICENSE /src/NOTICE /usr/share/licenses/signalbackup/
LABEL org.opencontainers.image.source="https://github.com/NaomiAmethyst/signalbackup" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
WORKDIR /data
USER 65532:65532
ENTRYPOINT ["/app/sigbackup"]
CMD ["--help"]
