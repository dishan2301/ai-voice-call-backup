#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
expected_dir="${HOME}/dishan/morning-mail-pa"
service_source="${project_dir}/deploy/morning-mail-pa.service"
service_target="${HOME}/.config/systemd/user/morning-mail-pa.service"

if [[ "${project_dir}" != "${expected_dir}" ]]; then
  echo "This service template expects the project at ${expected_dir}."
  echo "Move the project there or edit WorkingDirectory and ExecStart in ${service_source}."
  exit 1
fi

install -D -m 0644 "${service_source}" "${service_target}"
systemctl --user daemon-reload

if [[ "${1:-}" == "--no-start" ]]; then
  systemctl --user enable morning-mail-pa.service
  echo "Morning Mail PA will start at the next login."
else
  systemctl --user enable --now morning-mail-pa.service
  systemctl --user --no-pager status morning-mail-pa.service || true
fi

echo "Installed ${service_target}"
