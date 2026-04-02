#!/usr/bin/env bash
set -e

OUTPUT_DIR="$(pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR/build"

# Use the second line to avoid screen sleep in Gnome
YOMBIR_OUTPUT_DIR="$OUTPUT_DIR" exec ./yombir "$@"
#exec gnome-session-inhibit ./yombir "$@"
