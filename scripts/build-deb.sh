#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v dpkg-deb >/dev/null 2>&1 || { echo 'dpkg-deb is required; no packages installed automatically' >&2; exit 1; }
output=${1:-"$root/dist/session-search_0.7.3_all.deb"}
mkdir -p "$(dirname -- "$output")"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$stage/DEBIAN" "$stage/usr/lib/session-search/session_search" "$stage/usr/bin" "$stage/usr/lib/systemd/user" "$stage/usr/share/doc/session-search"
cp "$root/packaging/control" "$stage/DEBIAN/control"
# Explicit allowlist excludes sessions, databases, environments and model weights.
for name in __init__ reader store embedding indexer server tui tui_backend cpu charts search_history; do
    cp "$root/src/session_search/$name.py" "$stage/usr/lib/session-search/session_search/$name.py"
done
cp "$root/scripts/session-search" "$stage/usr/bin/session-search"
chmod 755 "$stage/usr/bin/session-search"
cp "$root/packaging/codex-session-index.service" "$stage/usr/lib/systemd/user/"
cp "$root/README.md" "$root/requirements-indexer.txt" "$root/requirements-tui.txt" "$stage/usr/share/doc/session-search/"
dpkg-deb --root-owner-group --build "$stage" "$output"
