#!/usr/bin/env bash
set -euo pipefail

client_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -r /etc/os-release ]]; then
    echo "error: this installer requires Ubuntu" >&2
    exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "error: this installer supports Ubuntu; detected ${PRETTY_NAME:-unknown Linux}" >&2
    exit 1
fi

packages=(
    python3-venv
    python3-tk
    libportaudio2
    pipewire
    pipewire-pulse
    pulseaudio-utils
    openssh-client
    libsecret-1-0
)
missing_packages=()

for package in "${packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fq 'install ok installed'; then
        missing_packages+=("$package")
    fi
done

if ((${#missing_packages[@]})); then
    echo "Installing Ubuntu packages: ${missing_packages[*]}"
    sudo apt-get update
    sudo apt-get install -y "${missing_packages[@]}"
else
    echo "Ubuntu packages are already installed."
fi

python3 -m venv "$client_dir/.venv"
"$client_dir/.venv/bin/python" -m pip install --upgrade pip
"$client_dir/.venv/bin/python" -m pip install -r "$client_dir/requirements.txt"
"$client_dir/install-gemini.sh"

systemctl --user start pipewire pipewire-pulse
"$client_dir/setup-virtual-mic.sh"

echo
echo "Linux client setup complete. Start it with: $client_dir/run-gui.sh"
