"""ffmpeg access without a system install: imageio-ffmpeg ships a static build."""
from __future__ import annotations

import functools
import shutil


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """Path of the ffmpeg binary bundled with imageio-ffmpeg (falls back to PATH)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("ffmpeg not found: run the app with `uv run server.py` so the "
                           "imageio-ffmpeg dependency is installed")
