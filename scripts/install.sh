#!/usr/bin/env bash
# Install claude-no-p.
#
# Installs the package into a venv at ~/.poor-claude/venv/ and creates a
# wrapper script at ~/.local/bin/claude-no-p (or a custom --bin-dir).
#
# Usage:
#   scripts/install.sh [options]
#
# Options:
#   --bin-dir DIR    Install wrapper to DIR (default: ~/.local/bin)
#   --name NAME      Wrapper executable name (default: claude-no-p)
#   --upgrade        Re-install/upgrade an existing installation
#   -h, --help       Show this help

set -euo pipefail

INSTALL_DIR="${HOME}/.poor-claude"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_DIR="${HOME}/.local/bin"
NAME="claude-no-p"
UPGRADE=0

usage() {
  # Print the leading comment block (up to the first non-comment line)
  while IFS= read -r line; do
    [[ "$line" == '#!/'* ]] && continue
    [[ "$line" == '#'* ]] || break
    stripped="${line#'#'}"; printf '%s\n' "${stripped# }"  # strip "# " or bare "#"
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
    --upgrade)
      UPGRADE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

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

# --- Install ------------------------------------------------------------------

printf 'Installing claude-no-p...\n'
printf '  Source:  %s\n' "${REPO_ROOT}"
printf '  Venv:    %s\n' "${VENV_DIR}"
printf '  Wrapper: %s/%s\n' "${BIN_DIR}" "${NAME}"
printf '\n'

# Create or recreate the venv
if [[ -d "${VENV_DIR}" ]]; then
  printf 'Upgrading existing venv...\n'
else
  printf 'Creating venv...\n'
  mkdir -p "${INSTALL_DIR}"
  python3 -m venv "${VENV_DIR}"
fi

# Install the package
printf 'Installing package...\n'
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -e "${REPO_ROOT}"

# Write the wrapper
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

# Warn if bin-dir is not on PATH
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  printf '\nNote: %s is not on your PATH.\n' "${BIN_DIR}"
  printf 'Add this to your shell profile:\n'
  printf '  export PATH="%s:$PATH"\n' "${BIN_DIR}"
fi
