#!/usr/bin/env bash
# Bootstrap claude-no-p from GitHub — no clone required.
#
# Usage (curl one-liner):
#   curl -fsSL https://raw.githubusercontent.com/HammerMei/poor-claude/main/scripts/bootstrap.sh | bash
#
# Pass options via bash -s:
#   curl -fsSL ... | bash -s -- --upgrade
#   curl -fsSL ... | bash -s -- --bin-dir /usr/local/bin
#
# Options:
#   --bin-dir DIR    Install wrapper to DIR (default: ~/.local/bin)
#   --name NAME      Wrapper executable name (default: claude-no-p)
#   --ref REF        Git ref to install (tag, branch, SHA; default: latest release tag)
#   --upgrade        Re-install/upgrade an existing installation
#   -h, --help       Show this help

set -euo pipefail

INSTALL_DIR="${HOME}/.poor-claude"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"
NAME="claude-no-p"
REF=""
UPGRADE=0
REPO="HammerMei/poor-claude"

usage() {
  while IFS= read -r line; do
    [[ "$line" == '#!/'* ]] && continue
    [[ "$line" == '#'* ]] || break
    stripped="${line#'#'}"; printf '%s\n' "${stripped# }"
  done < "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      [[ $# -lt 2 ]] && { printf 'error: --bin-dir requires a value\n' >&2; exit 2; }
      BIN_DIR="$2"; shift 2 ;;
    --name)
      [[ $# -lt 2 ]] && { printf 'error: --name requires a value\n' >&2; exit 2; }
      NAME="$2"; shift 2 ;;
    --ref)
      [[ $# -lt 2 ]] && { printf 'error: --ref requires a value\n' >&2; exit 2; }
      REF="$2"; shift 2 ;;
    --upgrade)
      UPGRADE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

# --- Checks -------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  printf 'error: python3 is required but was not found on PATH\n' >&2
  exit 1
fi

py_version=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
if [[ "$py_version" -lt 311 ]]; then
  printf 'error: Python 3.11+ is required (found %s)\n' \
    "$(python3 -c 'import sys; print(sys.version.split()[0])')" >&2
  exit 1
fi

if [[ -d "${VENV_DIR}" && "${UPGRADE}" -eq 0 ]]; then
  printf 'Already installed. Run with --upgrade to update.\n'
  printf '  Venv:    %s\n' "${VENV_DIR}"
  printf '  Wrapper: %s/%s\n' "${BIN_DIR}" "${NAME}"
  exit 0
fi

# --- Resolve ref --------------------------------------------------------------

if [[ -z "${REF}" ]]; then
  printf 'Fetching latest release tag...\n'
  REF=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || true)
  if [[ -z "${REF}" ]]; then
    printf 'warning: could not fetch latest release, falling back to main\n' >&2
    REF="main"
  fi
  printf '  Using ref: %s\n' "${REF}"
fi

# --- Install ------------------------------------------------------------------

PKG_URL="git+https://github.com/${REPO}.git@${REF}"

printf 'Installing claude-no-p...\n'
printf '  Package: %s\n' "${PKG_URL}"
printf '  Venv:    %s\n' "${VENV_DIR}"
printf '  Wrapper: %s/%s\n' "${BIN_DIR}" "${NAME}"
printf '\n'

if [[ -d "${VENV_DIR}" ]]; then
  printf 'Upgrading existing venv...\n'
else
  printf 'Creating venv...\n'
  mkdir -p "${INSTALL_DIR}"
  python3 -m venv "${VENV_DIR}"
fi

printf 'Installing package...\n'
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet --upgrade "${PKG_URL}"

printf 'Writing wrapper...\n'
mkdir -p "${BIN_DIR}"
PYTHON="${VENV_DIR}/bin/python"
cat > "${BIN_DIR}/${NAME}" <<WRAPPER
#!/usr/bin/env bash
exec '${PYTHON}' -m poor_claude.cli "\$@"
WRAPPER
chmod 0755 "${BIN_DIR}/${NAME}"

printf '\nDone!\n'
printf '  Run: %s "hello"\n' "${NAME}"

if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  printf '\nNote: %s is not on your PATH.\n' "${BIN_DIR}"
  printf 'Add this to your shell profile:\n'
  printf '  export PATH="%s:$PATH"\n' "${BIN_DIR}"
fi
