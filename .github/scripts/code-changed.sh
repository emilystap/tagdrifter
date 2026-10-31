#!/usr/bin/env bash
# Prints "true" if the commits from BASE to HEAD change anything other than documentation,
# "false" if they only touch the docs listed below. Used by the workflows to skip tests and
# image builds for docs-only changes.
#
# The list names what is definitely documentation; everything else counts as code, including
# workflows, the Dockerfile, dependencies and README.md (which the image build copies in).
# When in doubt (no usable base commit, empty diff) it prints "true", so checks run.
#
# Usage: code-changed.sh BASE_SHA
set -euo pipefail

base="${1:-}"
if [[ -z "$base" || "$base" =~ ^0+$ ]] || ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
  echo true
  exit 0
fi

changed="$(git diff --name-only "$base" HEAD)"
if [[ -z "$changed" ]]; then
  echo true
  exit 0
fi

while IFS= read -r path; do
  case "$path" in
    docs/* | CLAUDE.md | LICENSE) ;;
    *)
      echo true
      exit 0
      ;;
  esac
done <<< "$changed"
echo false
