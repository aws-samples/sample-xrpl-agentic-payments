#!/usr/bin/env bash
set -euo pipefail

# One-time, per-account-and-region setup. Without this, AgentCore Runtime,
# Gateway, and Memory tracing (all enabled by default in the CDK stack)
# silently produce no spans — CloudWatch Transaction Search is what lets
# X-Ray deliver trace data into CloudWatch Logs and the GenAI Observability
# dashboard in the first place. Safe to re-run; every step here is
# idempotent. Logs work without this step; only traces need it.
#
# This changes an account/region-wide X-Ray setting, not something scoped to
# this stack. If the account is shared with other workloads that manage
# their own X-Ray configuration, coordinate before running this.

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${repository_root}/scripts/_env_file.sh"
env_file="${ENV_FILE:-${repository_root}/.env}"

read_env_default AWS_DEFAULT_REGION "${env_file}"
if [[ -z "${AWS_DEFAULT_REGION:-}" ]]; then
  echo "AWS_DEFAULT_REGION is required. Set it in .env (see .env.example)." >&2
  exit 2
fi
region="${AWS_DEFAULT_REGION}"
account_id="$(aws sts get-caller-identity --region "${region}" --query Account --output text)"

policy_document=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TransactionSearchXRayAccess",
      "Effect": "Allow",
      "Principal": {"Service": "xray.amazonaws.com"},
      "Action": "logs:PutLogEvents",
      "Resource": [
        "arn:aws:logs:${region}:${account_id}:log-group:aws/spans:*",
        "arn:aws:logs:${region}:${account_id}:log-group:/aws/application-signals/data:*"
      ],
      "Condition": {
        "ArnLike": {"aws:SourceArn": "arn:aws:xray:${region}:${account_id}:*"},
        "StringEquals": {"aws:SourceAccount": "${account_id}"}
      }
    }
  ]
}
JSON
)

aws logs put-resource-policy \
  --region "${region}" \
  --policy-name "AgentCoreTransactionSearchXRayAccess" \
  --policy-document "${policy_document}" >/dev/null

# update-trace-segment-destination errors if already at the target value
# (unlike put-resource-policy above, it isn't a true PUT), so check first —
# this may already be done, whether by an earlier run of this script or by
# hand in the console.
current_destination="$(
  aws xray get-trace-segment-destination --region "${region}" --query Destination --output text
)"
if [[ "${current_destination}" != "CloudWatchLogs" ]]; then
  aws xray update-trace-segment-destination \
    --region "${region}" \
    --destination CloudWatchLogs >/dev/null
fi

echo "CloudWatch Transaction Search enabled for account ${account_id} in ${region}."
echo "Traces from AgentCore Runtime, Gateway, and Memory now land in CloudWatch."
