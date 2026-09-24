#!/usr/bin/env bash
set -euo pipefail

# AWS Agent Registry creates every record in DRAFT status, even with the
# registry's ApprovalConfiguration set to auto-approve everything —
# confirmed live: the four records this stack declares stayed DRAFT (and
# invisible to search_discoverable_registry_records, which only returns
# approved records) until each was explicitly submitted. Auto-approval only
# decides the outcome of a submission; it doesn't submit for you. This is a
# one-time step per record — run again after any change to a record's
# content, since a content update also resets it to DRAFT. Idempotent: an
# already-approved record submitted again just returns APPROVED.
#
# See
# https://docs.aws.amazon.com/agent-registry-control/latest/APIReference/API_SubmitRegistryRecordForApproval.html
# and docs/architecture.md#agent-registry.

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${repository_root}/scripts/_env_file.sh"
env_file="${ENV_FILE:-${repository_root}/.env}"
stack_name="${STACK_NAME:-XrplAgentCorePoc}"

read_env_default AWS_DEFAULT_REGION "${env_file}"
if [[ -z "${AWS_DEFAULT_REGION:-}" ]]; then
  echo "AWS_DEFAULT_REGION is required. Set it in .env (see .env.example)." >&2
  exit 2
fi
region="${AWS_DEFAULT_REGION}"

registry_id="$(
  aws cloudformation describe-stacks \
    --region "${region}" \
    --stack-name "${stack_name}" \
    --query "Stacks[0].Outputs[?OutputKey=='AgentRegistryId'].OutputValue" \
    --output text
)"
if [[ -z "${registry_id}" || "${registry_id}" == "None" ]]; then
  echo "Stack ${stack_name} has no AgentRegistryId output yet. Deploy first." >&2
  exit 2
fi

record_ids="$(
  aws agent-registry-control list-registry-records \
    --region "${region}" \
    --registry-id "${registry_id}" \
    --query 'registryRecords[].recordId' \
    --output text
)"

for record_id in ${record_ids}; do
  status="$(
    aws agent-registry-control submit-registry-record-for-approval \
      --region "${region}" \
      --registry-id "${registry_id}" \
      --record-id "${record_id}" \
      --query 'status' \
      --output text
  )"
  echo "${record_id}: ${status}"
done
