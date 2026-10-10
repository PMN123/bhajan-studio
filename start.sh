#!/bin/bash
# Bhajan Studio launcher for macOS.
#
#   ./start.sh                 start the app and open it in your browser
#   ./start.sh --port 9000     use another port (the next free one is used if it is busy)
#   ./start.sh --no-open       don't open the browser
#   ./start.sh --yes           don't ask before installing uv
#   ./start.sh --no-install    never install uv; only use a uv that is already on your PATH
#
# The only thing it may install is uv (https://docs.astral.sh/uv/), and it asks first.
# uv then fetches Python 3.12 and the Python packages into this folder's .venv, once.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

UV_INSTALL_CMD='curl -LsSf https://astral.sh/uv/install.sh | sh'

usage() {
  sed -n '4,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

say() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

ASSUME_YES=0
NO_INSTALL=0
OPEN_BROWSER=1
PORT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --no-install) NO_INSTALL=1 ;;
    --no-open) OPEN_BROWSER=0 ;;
    --port)
      [ $# -ge 2 ] || die "--port needs a number, e.g. --port 9000"
      PORT_ARG="$2"
      shift ;;
    --port=*) PORT_ARG="${1#--port=}" ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ -n "$PORT_ARG" ]; then
  case "$PORT_ARG" in
    *[!0-9]*) die "--port must be a number, not '$PORT_ARG'" ;;
  esac
  [ "$PORT_ARG" -ge 1 ] && [ "$PORT_ARG" -le 65535 ] || die "--port must be between 1 and 65535"
fi

if [ "$(uname -s)" != "Darwin" ]; then
  say "Note: Bhajan Studio is made for macOS; trying anyway."
else
  # The packages uv installs have no builds for older systems (it would try to compile SciPy and fail).
  macos_version="$(sw_vers -productVersion 2>/dev/null || true)"
  macos_major="$(printf '%s' "$macos_version" | cut -d. -f1)"
  macos_minor="$(printf '%s' "$macos_version" | cut -d. -f2)"
  case "$macos_major$macos_minor" in *[!0-9]*|"") macos_major=99; macos_minor=0 ;; esac
  if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then
    if [ "$macos_major" -lt 12 ]; then
      die "Bhajan Studio needs macOS 12 (Monterey) or later on Apple Silicon Macs; this Mac has macOS $macos_version. Update it in System Settings > Software Update, then try again."
    fi
  elif [ "$macos_major" -eq 10 ] && [ "$macos_minor" -lt 15 ]; then
    die "Bhajan Studio needs macOS 10.15 (Catalina) or later; this Mac has macOS $macos_version."
  fi
  # Files from a downloaded ZIP carry Apple's quarantine flag; clear it so macOS stops asking.
  if xattr -p com.apple.quarantine . >/dev/null 2>&1 \
      || xattr -p com.apple.quarantine start.sh >/dev/null 2>&1 \
      || xattr -p com.apple.quarantine start.command >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine . 2>/dev/null || true
  fi
fi

uv_instructions() {
  say ""
  say "Bhajan Studio needs uv, a small free tool that runs Python apps without touching the rest"
  say "of your Mac. To install it, open Terminal and run:"
  say ""
  say "    $UV_INSTALL_CMD"
  say ""
  say "then run ./start.sh again (or double-click start.command)."
  say "More about uv: https://docs.astral.sh/uv/getting-started/installation/"
}

UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ "$NO_INSTALL" = 1 ]; then
  say "uv was not found on your PATH."
  uv_instructions
  exit 1
fi
if [ -z "$UV" ]; then
  # Installed earlier but this shell hasn't picked up the new PATH yet?
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      UV="$candidate"
      PATH="$(dirname "$candidate"):$PATH"
      export PATH
      break
    fi
  done
fi
if [ -z "$UV" ]; then
  say "Bhajan Studio runs with uv, a small free tool that downloads and runs Python apps."
  say "It isn't installed yet. The official installer (from astral.sh, the makers of uv) puts it"
  say "in ~/.local/bin and needs no administrator password:"
  say ""
  say "    $UV_INSTALL_CMD"
  say ""
  if [ "$ASSUME_YES" = 1 ]; then
    answer=y
  elif [ -t 0 ]; then
    printf 'Install uv now? [Y/n] '
    read -r answer || answer=n
  else
    say "No terminal to ask in; run ./start.sh --yes to install without asking."
    exit 1
  fi
  case "$answer" in
    ""|[Yy]|[Yy][Ee][Ss]) ;;
    *)
      say "OK, not installing. To install uv later, run the command above in Terminal, then"
      say "start Bhajan Studio again."
      exit 1 ;;
  esac
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    say ""
    say "Installing uv failed (are you online?)."
    uv_instructions
    exit 1
  fi
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
  UV="$(command -v uv 2>/dev/null || true)"
  [ -n "$UV" ] || { say "uv was installed but can't be found; open a new Terminal window and try again."; exit 1; }
  say ""
fi

if [ ! -x .venv/bin/python ]; then
  say "First run: uv will now download Python 3.12 and the app's packages, including ffmpeg,"
  say "the Rubber Band pitch shifter and the macOS Vision bindings (about 110 MB to download,"
  say "about 400 MB on disk). This happens once: under a minute on a fast connection, a few"
  say "minutes on a slow one. Later starts take a few seconds."
  say ""
fi

# Only use uv's own Python builds: never the system /usr/bin/python3 (which asks to install the
# Xcode tools) or a Homebrew one. Pin 3.12, the version this app is tested with.
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-managed}"
# Slow connections: give package downloads more time before uv gives up.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-120}"

# --frozen: install exactly the committed uv.lock and never rewrite it.
set -- --frozen --python 3.12 server.py
if [ "$OPEN_BROWSER" = 1 ]; then set -- "$@" --open; fi
if [ -n "$PORT_ARG" ]; then set -- "$@" --port "$PORT_ARG"; fi
exec "$UV" run "$@"
