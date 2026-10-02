#!/usr/bin/env bash
set -euo pipefail

project="${1:-$PWD}"
archive="${2:?usage: package-site.sh PROJECT_DIR ARCHIVE_PATH}"
hosting="$project/.openai/hosting.json"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

test -f "$hosting" || { echo "Missing .openai/hosting.json" >&2; exit 2; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
build_kind="$(node "$script_dir/prepare-site-build.cjs" "$project" "$stage/dist")"
mkdir -p "$stage/dist/.openai"
if test "$build_kind" = worker; then
  cp "$hosting" "$stage/dist/.openai/hosting.json"
fi
if test -d "$project/drizzle"; then
  mkdir -p "$stage/dist/.openai/drizzle"
  cp -R "$project/drizzle"/. "$stage/dist/.openai/drizzle"/
fi

mkdir -p "$(dirname "$archive")"
tar -C "$stage" -czf "$archive" dist
archive_entries="$(tar -tzf "$archive")"
grep -qx 'dist/.openai/hosting.json' <<<"$archive_entries"
printf '%s\n' "$archive"
