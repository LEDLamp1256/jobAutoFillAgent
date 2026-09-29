#!/bin/zsh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
support="${JOBAGENT_SUPPORT_DIR:-$HOME/Library/Application Support/Job Application Agent}"
runtime="$support/runtime"
python="$runtime/python/bin/python"
cli="$runtime/playwright-mcp/node_modules/@playwright/mcp/cli.js"

command -v python3 >/dev/null || { print -u2 'Python 3 is required to prepare the local runtime.'; exit 1; }
command -v node >/dev/null || { print -u2 'Node is required to prepare the local runtime.'; exit 1; }
command -v npm >/dev/null || { print -u2 'npm is required to prepare the local runtime.'; exit 1; }

mkdir -p "$runtime/bin"
if [[ ! -x "$python" ]]; then
    python3 -m venv "$runtime/python"
fi
if ! "$python" -c 'import mcp, playwright, requests' >/dev/null 2>&1; then
    "$python" -m pip install -r "$repo_root/requirements.txt"
fi
if [[ ! -f "$cli" ]]; then
    npm install --prefix "$runtime/playwright-mcp" --no-save --no-audit --no-fund '@playwright/mcp@0.0.82'
fi

node_path="$(command -v node)"
if [[ -e "$runtime/bin/node" && ! -L "$runtime/bin/node" ]]; then
    print -u2 'The managed runtime/bin/node path is not a symlink; refusing to replace it.'
    exit 1
fi
ln -sfn "$node_path" "$runtime/bin/node"

[[ -x "$python" && -x "$runtime/bin/node" && -f "$cli" ]] || {
    print -u2 'The local Python/Node/MCP runtime is incomplete.'
    exit 1
}
print 'Local Job Application Agent runtime is ready in Application Support.'
