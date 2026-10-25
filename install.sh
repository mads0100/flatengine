#!/usr/bin/env bash

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATA_DIR="${HOME}/.local/share/flatengine"
SCRIPT_DIR="${DATA_DIR}/scripts"
TEMPLATE_DIR="${DATA_DIR}/templates"

BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/flatengine"
STATE_DIR="${HOME}/.local/state/flatengine"
QUEUE_DIR="${STATE_DIR}/queues"

LAUNCHER="${BIN_DIR}/flatengine"

echo
echo "========================================================================"
echo "FLATENGINE INSTALLER"
echo "========================================================================"
echo

# ----------------------------------------------------------------------
# Dependency checks
# ----------------------------------------------------------------------

missing=0

check_command() {
    local cmd="$1"

    if command -v "${cmd}" >/dev/null 2>&1; then
        printf "  %-16s %s\n" "${cmd}" "$(command -v "${cmd}")"
    else
        printf "  %-16s %s\n" "${cmd}" "MISSING"
        missing=1
    fi
}

echo "Checking required commands:"
check_command python3
check_command indi_getprop
check_command indi_setprop
check_command qdbus
check_command gdbus

echo
echo "Checking Python dependencies:"

if python3 - <<'PY'
import astropy
print("  astropy          OK")
PY
then
    :
else
    echo "  astropy          MISSING"
    missing=1
fi

if [[ "${missing}" -ne 0 ]]; then
    echo
    echo "Installation aborted: one or more dependencies are missing."
    exit 2
fi

# ----------------------------------------------------------------------
# Create installation directories
# ----------------------------------------------------------------------

mkdir -p \
    "${SCRIPT_DIR}" \
    "${TEMPLATE_DIR}" \
    "${BIN_DIR}" \
    "${CONFIG_DIR}" \
    "${QUEUE_DIR}"

# ----------------------------------------------------------------------
# Install scripts
# ----------------------------------------------------------------------

scripts=(
    flat_common.py
    flat-setup.py
    flat-calibrate.py
    flats-plan.py
    flats-execute.py
    flats-nightly.py
    flat-darkflats.py
    flats-regression.py
    flat-doctor.py
    session-stamp.py
    session-postcapture.py
)

echo
echo "Installing scripts:"

for name in "${scripts[@]}"; do
    src="${SOURCE_DIR}/${name}"
    dst="${SCRIPT_DIR}/${name}"

    if [[ ! -f "${src}" ]]; then
        echo "  Missing source file: ${src}"
        exit 2
    fi

    install -m 0755 "${src}" "${dst}"
    echo "  ${name}"
done

# ----------------------------------------------------------------------
# Install FlatEngine capture template
# ----------------------------------------------------------------------

template_src="${HOME}/.local/share/flatengine/templates/flat-calibration.esq"

if [[ ! -f "${template_src}" ]]; then
    # Development-tree fallback.
    template_src="${HOME}/observatory-queues/flat-calibration.esq"
fi

if [[ ! -f "${template_src}" ]]; then
    echo
    echo "Could not locate flat-calibration.esq template."
    exit 2
fi

template_dst="${TEMPLATE_DIR}/flat-calibration.esq"

if [[ "$(readlink -f "${template_src}")" == "$(readlink -f "${template_dst}")" ]]; then
    echo
    echo "Template already installed:"
    echo "  ${template_dst}"
else
    install -m 0644 \
        "${template_src}" \
        "${template_dst}"

    echo
    echo "Installed template:"
    echo "  ${template_dst}"
fi

# ----------------------------------------------------------------------
# Preserve/create configuration
# ----------------------------------------------------------------------

config_src="${HOME}/.config/flatengine/flat-config.json"
profiles_src="${HOME}/.config/flatengine/flat-filters.json"

if [[ -f "${config_src}" ]]; then
    echo
    echo "Existing config preserved:"
    echo "  ${config_src}"
else
    legacy="${HOME}/observatory-queues/flat-config.json"

    if [[ -f "${legacy}" ]]; then
        cp "${legacy}" "${config_src}"
        echo
        echo "Migrated existing config:"
        echo "  ${legacy}"
        echo "  -> ${config_src}"
    else
        echo
        echo "No existing flat-config.json found."
        echo "Run:"
        echo "  flatengine setup"
        echo "after installation."
    fi
fi

if [[ -f "${profiles_src}" ]]; then
    echo
    echo "Existing filter profiles preserved:"
    echo "  ${profiles_src}"
else
    legacy="${HOME}/observatory-queues/flat-filters.json"

    if [[ -f "${legacy}" ]]; then
        cp "${legacy}" "${profiles_src}"
        echo
        echo "Migrated filter profiles:"
        echo "  ${legacy}"
        echo "  -> ${profiles_src}"
    fi
fi

# ----------------------------------------------------------------------
# Install launcher
# ----------------------------------------------------------------------

launcher_src="${SOURCE_DIR}/flatengine"

if [[ -f "${launcher_src}" ]]; then
    install -m 0755 "${launcher_src}" "${LAUNCHER}"
else
    cat > "${LAUNCHER}" <<'LAUNCHER'
#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="${HOME}/.local/share/flatengine/scripts"

usage() {
    cat <<'EOF'
FlatEngine

Usage:
  flatengine setup
  flatengine plan [options]
  flatengine nightly [options]
  flatengine calibrate [options]
  flatengine execute [options]
  flatengine regression
  flatengine doctor
  flatengine stamp [options]
  flatengine help
EOF
}

cmd="${1:-help}"

if [[ $# -gt 0 ]]; then
    shift
fi

case "${cmd}" in
    setup)
        exec python3 "${SCRIPT_DIR}/flat-setup.py" "$@"
        ;;
    plan)
        exec python3 "${SCRIPT_DIR}/flats-plan.py" "$@"
        ;;
    nightly)
        exec python3 "${SCRIPT_DIR}/flats-nightly.py" "$@"
        ;;
    calibrate)
        exec python3 "${SCRIPT_DIR}/flat-calibrate.py" "$@"
        ;;
    execute)
        exec python3 "${SCRIPT_DIR}/flats-execute.py" "$@"
        ;;
    regression)
        exec python3 "${SCRIPT_DIR}/flats-regression.py" "$@"
        ;;
    doctor)
        exec python3 "${SCRIPT_DIR}/flat-doctor.py" "$@"
        ;;
    stamp)
        exec python3 "${SCRIPT_DIR}/session-stamp.py" "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "Unknown FlatEngine command: ${cmd}" >&2
        echo >&2
        usage >&2
        exit 2
        ;;
esac
LAUNCHER

    chmod +x "${LAUNCHER}"
fi

echo
echo "Installed launcher:"
echo "  ${LAUNCHER}"

# ----------------------------------------------------------------------
# PATH warning
# ----------------------------------------------------------------------

case ":${PATH}:" in
    *":${BIN_DIR}:"*)
        ;;
    *)
        echo
        echo "WARNING:"
        echo "  ${BIN_DIR}"
        echo "is not currently on PATH."
        echo
        echo "Add this to ~/.bashrc:"
        echo
        echo '  export PATH="$HOME/.local/bin:$PATH"'
        ;;
esac

# ----------------------------------------------------------------------
# Final verification
# ----------------------------------------------------------------------

echo
echo "Running syntax checks..."

python3 -m py_compile \
    "${SCRIPT_DIR}/flat_common.py" \
    "${SCRIPT_DIR}/flat-setup.py" \
    "${SCRIPT_DIR}/flat-calibrate.py" \
    "${SCRIPT_DIR}/flats-plan.py" \
    "${SCRIPT_DIR}/flats-execute.py" \
    "${SCRIPT_DIR}/flats-nightly.py" \
    "${SCRIPT_DIR}/flats-regression.py" \
    "${SCRIPT_DIR}/flat-doctor.py" \
    "${SCRIPT_DIR}/session-stamp.py"

echo "  Python syntax OK"

echo
echo "========================================================================"
echo "INSTALL COMPLETE"
echo "========================================================================"
echo
echo "Try:"
echo "  flatengine help"
echo "  flatengine regression"
echo
