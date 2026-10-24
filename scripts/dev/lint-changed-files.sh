#!/usr/bin/env bash
set -euo pipefail

base="${1:?usage: lint-changed-files.sh <diff-base>}"
paths_file=$(mktemp)
trap 'rm -f "${paths_file}"' EXIT

if ! git diff --name-only -z --diff-filter=ACMR "${base}...HEAD" >"${paths_file}" 2>/dev/null; then
  git diff --name-only -z --diff-filter=ACMR "${base}" >"${paths_file}"
fi

rc=0
while IFS= read -r -d '' file_path; do
  case "${file_path}" in
    .github/workflows/ci.yml|scripts/dev/lint-changed-files.sh) continue ;;
  esac
  [[ -f "${file_path}" ]] || continue
  if grep -nIE -- '/Users/cirwel|claude\.ai/code/session|Claude-Session:' "${file_path}" >/dev/null 2>&1; then
    printf '::error file=%s::operator-local path or Claude session-attribution link in committed content\n' "${file_path}"
    rc=1
  fi
done <"${paths_file}"

if [[ "${rc}" -eq 0 ]]; then
  echo "Files: clean"
fi
exit "${rc}"
