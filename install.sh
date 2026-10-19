#!/bin/sh
# Delegate a built module folder or ZIP to the installed Core lifecycle.
set -eu
case "$0" in */*) here=${0%/*} ;; *) here=. ;; esac
here=$(CDPATH= cd -- "$here" && pwd)
root=
source=$here
explicit_source=false
plan=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --root|--source)
            [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
            if [ "$1" = --root ]; then root=$2; else source=$2; explicit_source=true; fi
            shift 2 ;;
        --plan) plan=true; shift ;;
        -h|--help)
            echo 'Usage: install.sh [--root ROOT] [--source MODULE_FOLDER_OR_ZIP] [--plan]'
            echo 'Omit --root to use the root already bound in Core.'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
if ! command -v contextopus >/dev/null 2>&1; then
    echo 'Core 1.5+ is required. Install its starter first:' >&2
    echo 'https://github.com/pikiby/contextopus-core/releases' >&2
    exit 127
fi
if [ "$explicit_source" = false ] && [ ! -f "$source/module.json" ]; then
    echo 'Run this installer from an extracted module release, or pass --source to a built folder/ZIP.' >&2
    echo 'From source, first run: python3 scripts/build-module.py /path/to/new/session_search' >&2
    exit 2
fi
set -- modules install session_search --source "$source"
if [ "$plan" = true ]; then set -- "$@" --plan; fi
if [ -n "$root" ]; then set -- --root "$root" "$@"; fi
exec contextopus "$@"
