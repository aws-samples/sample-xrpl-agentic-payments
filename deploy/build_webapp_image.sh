#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# XRPL Agentic Payments web app image — build for arm64 in CodeBuild.
#
# The EKS nodes are Graviton (the Karpenter NodePool in infra/lib/eks-stack.ts
# requires "arm64"), and CDK does not build this image: eks-stack.ts names the
# ECR tag as a plain string, so an image has to exist in the repository before
# the Deployment can schedule. An amd64 image is worse than none — the pod
# starts and dies with an exec format error.
#
# This builds it WITHOUT a local container runtime, on a native arm64 CodeBuild
# host, so nothing has to be installed on a laptop:
#
#   1. assemble a build context zip from an explicit file list
#   2. refuse to upload it if it carries wallet seeds
#   3. upload to the build-source bucket
#   4. start the CodeBuild project and stream the result
#
# Requires XrplAgenticPaymentsStack to be deployed (it creates the bucket, the
# project and the ECR repository).
#
# Usage:
#   deploy/build_webapp_image.sh [image-tag]        # default tag: latest

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
STACK_NAME="${STACK_NAME:-XrplAgenticPaymentsStack}"
IMAGE_TAG="${1:-latest}"
REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"

BUILD_DIR="$SCRIPT_DIR/build-webapp"
ZIP="$SCRIPT_DIR/webapp-src.zip"

echo "=== Web app image build (arm64, via CodeBuild) ==="
echo "Stack:  $STACK_NAME"
echo "Region: $REGION"
echo "Tag:    $IMAGE_TAG"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Resolve the bucket and project from the stack, rather than hardcoding them
# ─────────────────────────────────────────────────────────────────────────────
stack_output() {
    aws cloudformation describe-stacks \
        --region "$REGION" \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
        --output text 2>/dev/null
}

BUCKET="$(stack_output WebappBuildSourceBucket)"
PROJECT="$(stack_output WebappBuildProject)"

if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ] || [ -z "$PROJECT" ] || [ "$PROJECT" = "None" ]; then
    echo "ERROR: could not read WebappBuildSourceBucket / WebappBuildProject from $STACK_NAME." >&2
    echo "Deploy the core stack first:" >&2
    echo "  cd infra && npx cdk deploy $STACK_NAME --context ..." >&2
    exit 1
fi

echo "Bucket:  $BUCKET"
echo "Project: $PROJECT"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Assemble the build context from an EXPLICIT list
# ─────────────────────────────────────────────────────────────────────────────
# An explicit list rather than "zip the repo minus some excludes": a new
# top-level directory holding something sensitive would be picked up silently by
# an exclude-based copy, and would not be by this.
#
# config/ is not here on purpose. It holds config/wallets.json — private XRPL
# seeds — and an image layer is not a trust boundary: anyone who can pull the
# image gets them, and `docker history` keeps them even if a later layer deletes
# the file. The tools read the seeds from Secrets Manager at invoke time.
echo ""
echo "--- Assembling build context ---"
rm -rf "$BUILD_DIR" "$ZIP"
mkdir -p "$BUILD_DIR"

for path in Dockerfile.webapp requirements.txt src webapp data; do
    if [ ! -e "$PROJECT_ROOT/$path" ]; then
        echo "ERROR: $path is missing from $PROJECT_ROOT — cannot build." >&2
        exit 1
    fi
    cp -R "$PROJECT_ROOT/$path" "$BUILD_DIR/$path"
    echo "  + $path"
done

find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc" -delete 2>/dev/null || true

cd "$BUILD_DIR"
zip -r "$ZIP" . -x "*.pyc" -x "__pycache__/*" > /dev/null
cd "$PROJECT_ROOT"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Refuse to upload secret material
# ─────────────────────────────────────────────────────────────────────────────
# Checked on the ARCHIVE, like deploy/package.sh, so it also catches anything a
# future step or a stale build-webapp/ directory drops in.
echo ""
echo "--- Verifying no secret material in the build context ---"
LEAKED=$(zipinfo -1 "$ZIP" | grep -E '(^|/)(wallets\.json|\.env(\..+)?|[^/]+\.secret)$' || true)
if [ -n "$LEAKED" ]; then
    echo "ERROR: refusing to upload a build context containing secret material:" >&2
    echo "$LEAKED" >&2
    rm -f "$ZIP"
    exit 1
fi
echo "OK: no wallet seeds, .env files or *.secret entries"
echo "Size: $(du -h "$ZIP" | cut -f1)"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Upload and build
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "--- Uploading to s3://$BUCKET/webapp-src.zip ---"
aws s3 cp "$ZIP" "s3://$BUCKET/webapp-src.zip" --region "$REGION" --only-show-errors

echo ""
echo "--- Starting CodeBuild ($PROJECT) ---"
BUILD_ID=$(aws codebuild start-build \
    --region "$REGION" \
    --project-name "$PROJECT" \
    --environment-variables-override "name=IMAGE_TAG,value=$IMAGE_TAG,type=PLAINTEXT" \
    --query 'build.id' --output text)
echo "Build: $BUILD_ID"

echo ""
echo "--- Waiting (arm64 build, typically 3-6 minutes) ---"
while true; do
    STATUS=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
        --query 'builds[0].buildStatus' --output text)
    PHASE=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
        --query 'builds[0].currentPhase' --output text)
    printf "\r  %s / %s          " "$STATUS" "$PHASE"
    [ "$STATUS" = "IN_PROGRESS" ] || break
    sleep 10
done
echo ""

if [ "$STATUS" != "SUCCEEDED" ]; then
    echo ""
    echo "ERROR: build $STATUS. Last 40 log lines:" >&2
    GROUP=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
        --query 'builds[0].logs.groupName' --output text)
    STREAM=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
        --query 'builds[0].logs.streamName' --output text)
    if [ "$GROUP" != "None" ] && [ "$STREAM" != "None" ]; then
        aws logs get-log-events --region "$REGION" \
            --log-group-name "$GROUP" --log-stream-name "$STREAM" \
            --limit 40 --query 'events[].message' --output text >&2 || true
    fi
    exit 1
fi

echo ""
echo "=== Image built and pushed ==="
aws ecr describe-images \
    --region "$REGION" \
    --repository-name xrpl-agentic-payments-webapp \
    --image-ids "imageTag=$IMAGE_TAG" \
    --query 'imageDetails[0].{Digest:imageDigest,Pushed:imagePushedAt,SizeMB:imageSizeInBytes}' \
    --output table 2>/dev/null || true

echo ""
echo "Roll the EKS Deployment onto it with:"
echo "  kubectl rollout restart deployment/xrpl-agentic-payments-web -n xrpl-agentic-payments"
