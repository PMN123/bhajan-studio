# Bhajan Studio, as a container.
#
#   docker build -t bhajan-studio .                                    # with Parts (CPU PyTorch)
#   docker build --build-arg WITH_PARTS=0 -t bhajan-studio:noparts .   # without Parts: much smaller
#   docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu130 .   # NVIDIA GPU
#   (or just: docker compose up -d --build, see docker-compose.yml)
#
# Works with BuildKit and with the classic builder (no RUN --mount, no heredocs).
# See docs/DEPLOY.md.
#
# LICENCES: Bhajan Studio's own code is MIT. The image also bundles GPL/AGPL software
# (a GPL build of ffmpeg inside imageio-ffmpeg, the Rubber Band library through pylibrb,
# PyMuPDF), and with Parts, diffq (CC BY-NC 4.0). Building and running it for yourself is
# fine; publishing the image is a decision for its owner (see "Licences" in docs/DEPLOY.md).

# ----------------------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

# uv, pinned. The interpreter is the one in the image (same path in both stages), never downloaded.
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

# What it takes to compile pylibrb. It has no linux/aarch64 wheel, so there it is built from its
# sdist, and that needs a C++ toolchain, fmt, and the Rubber Band library, version 4.0 or later.
# Debian bookworm only has 3.1, so Rubber Band is built here from its source release
# (checksummed), as a static library with its own FFT, and linked into pylibrb: nothing to
# install in the final image. On amd64 pylibrb comes as a self-contained wheel and all of this is
# skipped. None of the build tools reach the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config libfmt-dev meson ninja-build curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG RUBBERBAND_VERSION=4.0.0
ARG RUBBERBAND_SHA256=af050313ee63bc18b35b2e064e5dce05b276aaf6d1aa2b8a82ced1fe2f8028e9
RUN if [ "$(uname -m)" = "aarch64" ]; then \
        set -eux; \
        cd /tmp; \
        curl -fsSL -o rubberband.tar.bz2 "https://breakfastquay.com/files/releases/rubberband-${RUBBERBAND_VERSION}.tar.bz2"; \
        echo "${RUBBERBAND_SHA256}  rubberband.tar.bz2" | sha256sum -c -; \
        tar xjf rubberband.tar.bz2; \
        cd "rubberband-${RUBBERBAND_VERSION}"; \
        meson setup build --buildtype=release --prefix=/opt/rubberband --libdir=lib --default-library=static \
            -Dfft=builtin -Dresampler=builtin -Dcmdline=disabled -Dladspa=disabled -Dlv2=disabled \
            -Dvamp=disabled -Djni=disabled -Dtests=disabled; \
        ninja -C build install; \
        cd /tmp; rm -rf rubberband.tar.bz2 "rubberband-${RUBBERBAND_VERSION}"; \
    fi
ENV PKG_CONFIG_PATH=/opt/rubberband/lib/pkgconfig

WORKDIR /app

# Only the dependency description first, so editing the app does not reinstall the packages.
COPY pyproject.toml uv.lock ./
RUN uv venv --python /usr/local/bin/python3.12 /app/.venv

# 1. The locked base dependencies (the macOS-only pyobjc lines drop out on Linux).
#    The build constraint matters only where a package is compiled from source (pylibrb on
#    aarch64): its sdist asks for "nanobind >=1.5.2", and nanobind 2 removed the nb::any it uses,
#    so the build must use 1.x.
RUN echo 'nanobind==1.9.2' > /app/build-constraints.txt
ENV UV_BUILD_CONSTRAINT=/app/build-constraints.txt
RUN uv export --frozen --no-hashes --no-header --no-emit-project --no-dev -o /tmp/base.txt \
 && uv pip install --no-deps -r /tmp/base.txt

# 2. Parts (the locked "stems" extra): PyTorch from TORCH_INDEX (the CPU build by default, far
#    smaller than the CUDA build on PyPI), at the version uv.lock pins; everything else from PyPI
#    at the locked versions, installed with --no-deps so nothing is resolved again.
#    The torch, torchvision, nvidia-*, cuda-* and triton lines are dropped from the export: they
#    are the CUDA 13 builds the lock chose for PyPI. For a GPU build, the torch install below
#    pulls in the CUDA libraries that match the TORCH_INDEX it is given by itself.
ARG WITH_PARTS=1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN set -eux; \
    if [ "$WITH_PARTS" = "1" ]; then \
        uv export --frozen --no-hashes --no-header --no-emit-project --no-dev --extra stems -o /tmp/stems-all.txt; \
        grep -Ev '^(torch|torchvision|triton|nvidia-[A-Za-z0-9_.-]+|cuda-[A-Za-z0-9_.-]+)==' /tmp/stems-all.txt > /tmp/stems.txt; \
        TORCH_VER="$(sed -n 's/^torch==\([^ ;]*\).*/\1/p' /tmp/stems-all.txt | head -n 1)"; \
        TV_VER="$(sed -n 's/^torchvision==\([^ ;]*\).*/\1/p' /tmp/stems-all.txt | head -n 1)"; \
        test -n "$TORCH_VER" -a -n "$TV_VER"; \
        uv pip install --index-url "$TORCH_INDEX" --extra-index-url https://pypi.org/simple \
            --index-strategy unsafe-best-match -c /tmp/stems.txt \
            "torch==$TORCH_VER" "torchvision==$TV_VER"; \
        uv pip install --no-deps -r /tmp/stems.txt; \
        uv pip check; \
        python -c "import torch, torchvision; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'torchvision', torchvision.__version__)"; \
        case "$TORCH_INDEX" in \
            */cpu) python -c "import torch; assert '+cpu' in torch.__version__ and torch.version.cuda is None, torch.__version__" ;; \
        esac; \
        python -c "import audio_separator, onnxruntime, librosa, soundfile; print('audio-separator imports ok')"; \
    else \
        uv pip check; \
    fi

# Smaller image: drop test suites (about 230 MB, mostly torch's compiled C++ tests), PyTorch's C++
# headers and static libraries. Nothing here is imported at run time.
RUN find /app/.venv/lib -depth \( -type d -name tests -o -type d -name test \) -path '*/site-packages/*/*' \
        -not -path '*/site-packages/torch/testing*' -exec rm -rf {} + 2>/dev/null; \
    rm -rf /app/.venv/lib/python3.12/site-packages/torch/include \
           /app/.venv/lib/python3.12/site-packages/torch/share; \
    find /app/.venv -name '*.a' -delete; \
    true

# ----------------------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Bhajan Studio" \
      org.opencontainers.image.description="Transpose Sai Rhythms bhajans to any key: read the sheet music, synthesize it, or pitch-shift the real recording." \
      org.opencontainers.image.source="https://github.com/PMN123/bhajan-studio" \
      org.opencontainers.image.licenses="MIT"

# tesseract: reads the printed labels on the sheet music (BHAJAN_OCR=auto finds it on PATH).
# libgomp1: OpenMP runtime used by PyTorch, ONNX Runtime and scikit-learn.
# libfmt9: what pylibrb links to where it was compiled here (aarch64); the amd64 wheel carries its own.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libgomp1 libfmt9 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin bhajan \
 && mkdir /data \
 && chown bhajan:bhajan /data

COPY --from=builder /app/.venv /app/.venv

WORKDIR /app
COPY server.py pyproject.toml uv.lock LICENSE README.md ./
COPY bhajan ./bhajan
COPY static ./static
RUN python -m compileall -q bhajan

ENV PATH=/app/.venv/bin:$PATH \
    BHAJAN_DATA=/data \
    BHAJAN_HOST=0.0.0.0 \
    PORT=8777 \
    BHAJAN_SITE_GAP=10 \
    BHAJAN_OCR=auto \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/data/cache

USER bhajan
VOLUME /data
EXPOSE 8777

# A quick check that the native packages load here (a broken wheel or a missing library fails the
# build, not the first request). Imports only: nothing is written to /data.
ARG WITH_PARTS=1
RUN python -c "import fastapi, uvicorn, numpy, scipy, fitz, PIL, certifi, pylibrb, imageio_ffmpeg" \
 && if [ "$WITH_PARTS" = "1" ]; then python -c "import torch, torchvision, audio_separator, onnxruntime, librosa, soundfile, numba"; fi

# The health endpoint needs no password. Plain python, so the image needs no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8777/api/health', timeout=4).status == 200 else 1)"]

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8777"]
