#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
region="${AWS_DEFAULT_REGION:-us-west-2}"
stack_name="${STACK_NAME:-XrplAgentCorePoc}"
image_tag="${RUNTIME_IMAGE_TAG:-$(git -C "${repository_root}" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"

if [[ "${region}" != "us-west-2" ]]; then
  echo "This POC deploy script supports only us-west-2." >&2
  exit 2
fi

required=(
  XRPL_EXECUTION_ADDRESS
  XRPL_PAYOUT_ADDRESS
  XRPL_FEE_PAYER_ADDRESS
  XRPL_FEE_MERCHANT_ADDRESS
  XRPL_USD_ISSUER_ADDRESS
  XRPL_MXN_ISSUER_ADDRESS
)
# Exported values win; otherwise read each address from .env, which
# scripts/verify_and_set_env.py writes. Only these keys are read, so local-only
# settings in .env (such as ALLOW_DEMO_AUTH) never reach the deployment.
env_file="${ENV_FILE:-${repository_root}/.env}"
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" && -f "${env_file}" ]]; then
    value="$(grep -E "^(export[[:space:]]+)?${name}=" "${env_file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%\"}"; value="${value#\"}"
    if [[ -n "${value}" ]]; then
      printf -v "${name}" '%s' "${value}"
      export "${name}"
    fi
  fi
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required. Run: uv run python scripts/verify_and_set_env.py" >&2
    exit 2
  fi
done

cd "${repository_root}"
if [[ -n "$(git status --porcelain)" && "${ALLOW_DIRTY_DEPLOY:-false}" != "true" ]]; then
  echo "Refusing to deploy an uncommitted tree. Commit first or set ALLOW_DIRTY_DEPLOY=true." >&2
  exit 2
fi
"${repository_root}/scripts/build-lambda-layer.sh"
"${repository_root}/scripts/verify.sh"

deploy_stack() {
  local deploy_runtime="$1"
  npx cdk deploy "${stack_name}" \
    --app "npx ts-node --prefer-ts-exts infra/bin/app.ts" \
    --require-approval never \
    --parameters "DeployAgentRuntime=${deploy_runtime}" \
    --parameters "XrplExecutionAddress=${XRPL_EXECUTION_ADDRESS}" \
    --parameters "XrplPayoutAddress=${XRPL_PAYOUT_ADDRESS}" \
    --parameters "XrplFeePayerAddress=${XRPL_FEE_PAYER_ADDRESS}" \
    --parameters "XrplFeeMerchantAddress=${XRPL_FEE_MERCHANT_ADDRESS}" \
    --parameters "XrplUsdIssuerAddress=${XRPL_USD_ISSUER_ADDRESS}" \
    --parameters "XrplMxnIssuerAddress=${XRPL_MXN_ISSUER_ADDRESS}" \
    -c "runtimeImageTag=${image_tag}"
}

stack_exists=false
if aws cloudformation describe-stacks \
  --region "${region}" \
  --stack-name "${stack_name}" >/dev/null 2>&1; then
  stack_exists=true
fi

if [[ "${stack_exists}" != "true" ]]; then
  deploy_stack false
fi

repository_uri="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeRepositoryUri'].OutputValue | [0]" \
    --output text
)"
account_id="${repository_uri%%.*}"
aws ecr get-login-password --region "${region}" |
  docker login --username AWS --password-stdin \
    "${account_id}.dkr.ecr.${region}.amazonaws.com"
docker buildx build \
  --platform linux/arm64 \
  --file "${repository_root}/Dockerfile.runtime" \
  --tag "${repository_uri}:${image_tag}" \
  --push \
  "${repository_root}"

deploy_stack true

runtime_arn="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue | [0]" \
    --output text
)"
runtime_url="$(
  RUNTIME_ARN="${runtime_arn}" python3 -c \
    'import os, urllib.parse; print("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/" + urllib.parse.quote(os.environ["RUNTIME_ARN"], safe="") + "/invocations?qualifier=default")'
)"
api_url="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue | [0]" \
    --output text
)"
user_pool_id="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]" \
    --output text
)"
user_pool_client_id="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue | [0]" \
    --output text
)"

echo "Runtime image: ${repository_uri}:${image_tag}"
echo "AGENTCORE_RUNTIME_URL=${runtime_url}"
echo "NEXT_PUBLIC_API_BASE_URL=${api_url}"
echo "NEXT_PUBLIC_COGNITO_USER_POOL_ID=${user_pool_id}"
echo "NEXT_PUBLIC_COGNITO_CLIENT_ID=${user_pool_client_id}"
