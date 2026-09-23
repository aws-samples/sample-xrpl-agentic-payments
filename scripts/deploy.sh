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

runtime_arn="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeArn'].OutputValue | [0]" \
    --output text 2>/dev/null || true
)"
if [[ "${runtime_arn}" != arn:* ]]; then
  # No stack yet, or a stack whose Runtime pass never completed (for example,
  # an earlier run that failed before the remote build below): safe to
  # (re)apply the base resources — ECR, the CodeBuild project, its S3 source
  # bucket — with DeployAgentRuntime=false. Skipped once the Runtime exists,
  # so a rebuild never regresses a healthy stack back to no-Runtime.
  deploy_stack false
fi

repository_uri="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeRepositoryUri'].OutputValue | [0]" \
    --output text
)"
build_project_name="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeBuildProjectName'].OutputValue | [0]" \
    --output text
)"
build_source_bucket="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='RuntimeBuildSourceBucket'].OutputValue | [0]" \
    --output text
)"

# Build and push the Runtime image with AWS CodeBuild instead of a local
# container engine. The zip carries exactly what Dockerfile.runtime COPYs
# (also what .dockerignore allows through), so no repository secrets or
# wallet fixtures are ever uploaded.
build_source_zip="$(mktemp -d)/source.zip"
(cd "${repository_root}" && zip -q -X -r "${build_source_zip}" \
  Dockerfile.runtime pyproject.toml README.md src)
aws s3 cp "${build_source_zip}" "s3://${build_source_bucket}/runtime-build/source.zip" \
  --region "${region}"
rm -rf "$(dirname "${build_source_zip}")"

build_id="$(
  aws codebuild start-build \
    --region "${region}" \
    --project-name "${build_project_name}" \
    --environment-variables-override \
      "name=REPO_URI,value=${repository_uri},type=PLAINTEXT" \
      "name=IMAGE_TAG,value=${image_tag},type=PLAINTEXT" \
    --query "build.id" --output text
)"
echo "Started remote build ${build_id} on ${build_project_name}..."

build_status="IN_PROGRESS"
while [[ "${build_status}" == "IN_PROGRESS" ]]; do
  sleep 10
  build_status="$(
    aws codebuild batch-get-builds --region "${region}" --ids "${build_id}" \
      --query "builds[0].buildStatus" --output text
  )"
done
if [[ "${build_status}" != "SUCCEEDED" ]]; then
  echo "Remote build ${build_id} finished with status ${build_status}." >&2
  echo "Logs: aws codebuild batch-get-builds --region ${region} --ids ${build_id} --query 'builds[0].logs'" >&2
  exit 1
fi

deploy_stack true

echo "Runtime image: ${repository_uri}:${image_tag}"

# Writes web/.env.local from the stack's own outputs (API URL, Cognito IDs,
# Runtime URL) plus the fixture recipient in .env. Requires
# scripts/verify_and_set_env.py to have already set NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS.
STACK_NAME="${stack_name}" AWS_DEFAULT_REGION="${region}" \
  uv run python "${repository_root}/scripts/write_web_env.py"
