#!/usr/bin/env bash
# Shared helper for scripts/deploy.sh and scripts/create_poc_user.sh.
#
# read_env_default NAME ENV_FILE sets the shell variable NAME from ENV_FILE
# (a dotenv-style file) if NAME is not already exported. An exported value
# always wins. This is how AWS_DEFAULT_REGION and the XRPL wallet addresses
# have exactly one source of truth — .env — instead of each script carrying
# its own hardcoded default.
read_env_default() {
  local name="$1" env_file="$2" value
  if [[ -z "${!name:-}" && -f "${env_file}" ]]; then
    value="$(grep -E "^(export[[:space:]]+)?${name}=" "${env_file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%\"}"; value="${value#\"}"
    if [[ -n "${value}" ]]; then
      printf -v "${name}" '%s' "${value}"
      export "${name}"
    fi
  fi
}
