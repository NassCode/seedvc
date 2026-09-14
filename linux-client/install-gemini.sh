#!/usr/bin/env bash
set -euo pipefail

client_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tools_dir="$client_dir/.tools"
node_dir="$tools_dir/node"
gemini_dir="$tools_dir/gemini"
node_version="22.23.2"
node_archive="node-v${node_version}-linux-x64.tar.xz"
node_url="https://nodejs.org/dist/v${node_version}/${node_archive}"
node_sha256="d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307"

case "$(uname -m)" in
    x86_64) ;;
    *)
        echo "error: the bundled Gemini installer currently supports x86_64 Linux" >&2
        exit 1
        ;;
esac

mkdir -p "$tools_dir"
if [[ ! -x "$node_dir/bin/node" ]]; then
    archive_path="$tools_dir/$node_archive"
    curl -fL "$node_url" -o "$archive_path"
    printf '%s  %s\n' "$node_sha256" "$archive_path" | sha256sum --check --status
    mkdir -p "$node_dir"
    tar -xJf "$archive_path" --strip-components=1 -C "$node_dir"
    unlink "$archive_path"
fi

export PATH="$node_dir/bin:$PATH"
"$node_dir/bin/npm" install --prefix "$gemini_dir" @google/gemini-cli@latest

echo "Gemini CLI installed: $gemini_dir/node_modules/.bin/gemini"
