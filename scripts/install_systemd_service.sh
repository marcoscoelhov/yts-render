#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="yts-render-hub.service"
SERVICE_TEMPLATE="${REPO_ROOT}/deploy/systemd/${SERVICE_NAME}.in"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
RELOAD_SERVICE_NAME="yts-render-hub-reload.service"
RELOAD_PATH_NAME="yts-render-hub-reload.path"
RELOAD_SERVICE_TEMPLATE="${REPO_ROOT}/deploy/systemd/${RELOAD_SERVICE_NAME}.in"
RELOAD_PATH_TEMPLATE="${REPO_ROOT}/deploy/systemd/${RELOAD_PATH_NAME}.in"
RELOAD_SERVICE_DST="/etc/systemd/system/${RELOAD_SERVICE_NAME}"
RELOAD_PATH_DST="/etc/systemd/system/${RELOAD_PATH_NAME}"
HEALTH_URL="http://127.0.0.1:8080/healthz"

if [[ ! -f "${SERVICE_TEMPLATE}" ]]; then
  echo "service template not found: ${SERVICE_TEMPLATE}" >&2
  exit 1
fi

for template in "${RELOAD_SERVICE_TEMPLATE}" "${RELOAD_PATH_TEMPLATE}"; do
  if [[ ! -f "${template}" ]]; then
    echo "unit template not found: ${template}" >&2
    exit 1
  fi
done

if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  echo "missing venv python: ${REPO_ROOT}/.venv/bin/python" >&2
  exit 1
fi

rendered_service="$(mktemp --suffix=.service)"
rendered_reload_service="$(mktemp --suffix=.service)"
rendered_reload_path="$(mktemp --suffix=.path)"
trap 'rm -f "${rendered_service}" "${rendered_reload_service}" "${rendered_reload_path}"' EXIT

sed "s|__REPO_ROOT__|${REPO_ROOT}|g" "${SERVICE_TEMPLATE}" > "${rendered_service}"
sed "s|__REPO_ROOT__|${REPO_ROOT}|g" "${RELOAD_SERVICE_TEMPLATE}" > "${rendered_reload_service}"
sed "s|__REPO_ROOT__|${REPO_ROOT}|g" "${RELOAD_PATH_TEMPLATE}" > "${rendered_reload_path}"

if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${rendered_service}"
  systemd-analyze verify "${rendered_reload_service}" "${rendered_reload_path}"
fi

install -m 0644 "${rendered_service}" "${SERVICE_DST}"
install -m 0644 "${rendered_reload_service}" "${RELOAD_SERVICE_DST}"
install -m 0644 "${rendered_reload_path}" "${RELOAD_PATH_DST}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl enable --now "${RELOAD_PATH_NAME}"
systemctl restart "${SERVICE_NAME}"

for _ in {1..60}; do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -fsS "${HEALTH_URL}" >/dev/null
systemctl --no-pager --full status "${SERVICE_NAME}"
systemctl --no-pager --full status "${RELOAD_PATH_NAME}"
