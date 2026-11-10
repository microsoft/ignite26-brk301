#!/usr/bin/env bash
set -euo pipefail

if ! command -v azd >/dev/null 2>&1; then
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl --fail --silent --show-error --location https://aka.ms/install-azd.sh --output "$installer"
  bash "$installer"
fi

printf '\nBRK301 workspace ready. Open docs/deploy.md to choose a scenario.\n'
printf 'No sign-in, cloud provisioning, agent hiring, or messages were performed.\n'
