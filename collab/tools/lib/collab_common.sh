#!/usr/bin/env bash
# collab_common.sh — path resolution for the bash shim ONLY.
#
# INTENTIONALLY THIN. This file must NOT reimplement locking, atomic IO, or slugify —
# those live once in collab_common.py (single source of truth for concurrency-critical
# logic). This file exists solely so the bash `handoff` shim can locate KIT_ROOT and
# COLLAB_HOME the same way the Python core does (Amendment B naming).
#
# Sourced, not executed. Provides: SCRIPT_PATH, SCRIPT_DIR, KIT_ROOT, TOOLS_DIR,
# COLLAB_HOME (bash-local), and collab_root <name>.

set -euo pipefail

# --- helpers ---------------------------------------------------------------- #

# Resolve a path's real location using Python's os.path.realpath. Unlike MSYS `readlink -f`,
# this FOLLOWS native Windows reparse-point symlinks (e.g. an `install.sh`-created ~/bin
# link), so bash and the Python core agree on symlinked deployments (fixes DEFECT B).
_pyrealpath() {
  local py p
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
      if p="$("$py" -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null)"; then
        printf '%s\n' "$p"
        return 0
      fi
    fi
  done
  return 1
}

# Convert a (possibly Windows-form) path to MSYS `/c/..` form for bash path ops.
_to_msys() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1"
  else
    printf '%s\n' "$1"
  fi
}

# --- SCRIPT_PATH / SCRIPT_DIR of the caller (symlinks followed) -------------- #
# BASH_SOURCE[1] is the sourcing script; fall back to BASH_SOURCE[0] if sourced directly.
_caller="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
if _real="$(_pyrealpath "$_caller")"; then
  SCRIPT_PATH="$(_to_msys "$_real")"
else
  SCRIPT_PATH="$(readlink -f "$_caller")"   # fallback (no python / non-symlinked)
fi
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"

# --- KIT_ROOT: walk upward for the root signature -------------------------- #
# Does NOT assume a fixed depth — a shim is in tools/, the core in tools/lib/.
_resolve_kit_root() {
  local d="$1" i=0
  while [ "$i" -le 6 ]; do
    if [ -e "$d/.collab-kit" ] || { [ -d "$d/tools" ] && [ -e "$d/install.sh" ]; }; then
      printf '%s\n' "$d"
      return 0
    fi
    local parent
    parent="$(dirname "$d")"
    [ "$parent" = "$d" ] && break   # reached filesystem root
    d="$parent"
    i=$((i + 1))
  done
  echo "collab_common.sh: could not locate collab-kit root above $1" >&2
  return 1
}

# COLLAB_KIT_ROOT env override (what install.sh embeds) wins over runtime resolution. This
# is the robust path on Windows/Git-Bash, where `ln -s` copies-or-creates-unfollowable-links
# and runtime symlink resolution of a ~/bin shim cannot be trusted (DEFECT B). install.sh
# should generate the shim with an absolute COLLAB_KIT_ROOT rather than rely on a symlink.
if [ -n "${COLLAB_KIT_ROOT:-}" ]; then
  KIT_ROOT="$(_to_msys "$COLLAB_KIT_ROOT")"
else
  KIT_ROOT="$(_resolve_kit_root "$SCRIPT_DIR")"
fi
TOOLS_DIR="$KIT_ROOT/tools"

# --- COLLAB_HOME ------------------------------------------------------------ #
# If the user set COLLAB_HOME, LEAVE IT VERBATIM: the Python core canonicalizes it (and
# normalizes an MSYS `/c/..` form). We must NOT rewrite it to `readlink -f` MSYS form and
# re-export it — a native-Python child would then mis-resolve `/c/..` to `C:\c\..`,
# splitting the lock dir across entry points (DEFECT A). If unset, provide a bash-local
# fallback for collab_root() but DO NOT export it, so the Python child self-resolves from
# its own __file__.
if [ -z "${COLLAB_HOME:-}" ]; then
  COLLAB_HOME="$KIT_ROOT"   # bash-local only; intentionally not exported
fi

export SCRIPT_PATH SCRIPT_DIR KIT_ROOT TOOLS_DIR

# collab_root <name>: path to a collab under COLLAB_HOME (bash-side lookup/display).
# Real filesystem-name sanitization is delegated to the Python core's slugify().
collab_root() {
  printf '%s\n' "$COLLAB_HOME/$1"
}
