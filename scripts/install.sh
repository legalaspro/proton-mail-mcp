#!/usr/bin/env bash
# Install or remove Proton Mail Bridge, proton-mail-mcp and their services on
# Linux (Debian/Ubuntu, Fedora/RHEL, Arch) and macOS, from a checkout. Windows:
# scripts/install.ps1. From an installed wheel, `proton-mail-mcp setup` does this.
#
#   scripts/install.sh install --dry-run --bridge-user you@example.com
#   scripts/install.sh install --bridge-user you@example.com
#   scripts/install.sh uninstall --dry-run
#   scripts/install.sh detect
#
# This script only locates Python 3.12 or newer. The plan, every change and the
# --dry-run output come from the standard-library module proton_mail_mcp.installer
# in this checkout; see docs/install.md. Run it as your normal user: package
# steps call sudo themselves. Compatible with the bash 3.2 that macOS ships.
set -euo pipefail

# Builtins only until Python is found, so a bare PATH still reaches the hint.
case "${BASH_SOURCE[0]}" in
  */*) here="${BASH_SOURCE[0]%/*}" ;;
  *) here="." ;;
esac
root="$(cd "$here/.." && pwd -P)"

case "${OSTYPE:-}" in
  msys* | cygwin* | win32*)
    echo "install.sh: on Windows, run scripts/install.ps1 in PowerShell instead" >&2
    exit 2
    ;;
esac

python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate found
  for candidate in "${PMM_PYTHON:-}" python3.14 python3.13 python3.12 python3; do
    [ -n "$candidate" ] || continue
    found="$(command -v "$candidate" 2>/dev/null)" || continue
    if python_ok "$found"; then
      printf '%s\n' "$found"
      return 0
    fi
  done
  return 1
}

python_hint() {
  local id="" like="" key value release="${PMM_OS_RELEASE:-/etc/os-release}"
  case "${OSTYPE:-}" in
    darwin*)
      echo "  brew install python@3.12    (or the macOS installer from python.org)"
      return 0
      ;;
  esac
  if [ -r "$release" ]; then
    while IFS='=' read -r key value; do
      value="${value%\"}"
      value="${value#\"}"
      case "$key" in
        ID) id="$value" ;;
        ID_LIKE) like="$value" ;;
      esac
    done <"$release"
  fi
  case " $id $like " in
    *" debian "* | *" ubuntu "*)
      echo "  sudo apt-get install python3 python3-venv    (when Python 3.12+ is available)"
      echo "  on older releases, use uv's managed Python or install Python 3.12+ separately"
      ;;
    *" fedora "* | *" rhel "* | *" centos "*)
      echo "  sudo dnf install python3    (RHEL 9 and clones: python3.12)"
      ;;
    *" arch "*)
      echo "  sudo pacman -S python"
      ;;
    *)
      echo "  install Python 3.12 or newer with your package manager"
      ;;
  esac
}

if ! python="$(find_python)"; then
  echo "install.sh: Python 3.12 or newer is required. Install it, for example:" >&2
  python_hint >&2
  echo "then rerun this script, or set PMM_PYTHON to the interpreter's path." >&2
  exit 3
fi

# -P keeps the current directory off sys.path and -s ignores user site-packages,
# so only this checkout's src provides the installer.
export PYTHONPATH="$root/src"
export PYTHONDONTWRITEBYTECODE=1
exec "$python" -P -s -m proton_mail_mcp.installer "$@"
