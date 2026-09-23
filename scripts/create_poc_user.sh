#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${repository_root}/scripts/_env_file.sh"
env_file="${ENV_FILE:-${repository_root}/.env}"

# AWS_DEFAULT_REGION has exactly one source of truth: .env (see
# .env.example). No hardcoded default here — matches whichever region
# scripts/deploy.sh deployed the stack to.
read_env_default AWS_DEFAULT_REGION "${env_file}"
if [[ -z "${AWS_DEFAULT_REGION:-}" ]]; then
  echo "AWS_DEFAULT_REGION is required. Set it in .env (see .env.example)." >&2
  exit 2
fi
region="${AWS_DEFAULT_REGION}"
stack_name="${STACK_NAME:-XrplAgentCorePoc}"
email="${POC_USER_EMAIL:-}"
password="${POC_USER_PASSWORD:-}"

if [[ -z "${email}" || -z "${password}" ]]; then
  echo "POC_USER_EMAIL and POC_USER_PASSWORD are required." >&2
  exit 2
fi

user_pool_id="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" \
    --output text
)"
if [[ -z "${user_pool_id}" || "${user_pool_id}" == "None" ]]; then
  echo "The deployed stack did not return a Cognito user pool ID." >&2
  exit 1
fi

if ! aws cognito-idp admin-get-user \
  --region "${region}" \
  --user-pool-id "${user_pool_id}" \
  --username "${email}" >/dev/null 2>&1; then
  aws cognito-idp admin-create-user \
    --region "${region}" \
    --user-pool-id "${user_pool_id}" \
    --username "${email}" \
    --user-attributes \
      "Name=email,Value=${email}" \
      "Name=email_verified,Value=true" \
    --message-action SUPPRESS >/dev/null
fi

aws cognito-idp admin-set-user-password \
  --region "${region}" \
  --user-pool-id "${user_pool_id}" \
  --username "${email}" \
  --password "${password}" \
  --permanent

echo "POC Cognito user is ready: ${email}"
