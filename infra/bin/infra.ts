#!/usr/bin/env node

/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { Aspects } from "aws-cdk-lib";
import { AwsSolutionsChecks, NagSuppressions } from "cdk-nag";
import { XrplAgenticPaymentsStack } from "../lib/infra-stack";
import { XrplAgenticPaymentsWebStack } from "../lib/web-stack";
import { XrplAgenticPaymentsEksStack } from "../lib/eks-stack";
import { XrplAgenticPaymentsToolsStack } from "../lib/tools-stack";

const app = new cdk.App();

// Deploys to whatever account/region your AWS CLI credentials resolve to
// by default. Override explicitly with CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION
// (these are set automatically by `cdk deploy` from your current profile),
// or pass --context account=... --context region=...
const ENV = {
  account: app.node.tryGetContext("account") ?? process.env.CDK_DEFAULT_ACCOUNT,
  region: app.node.tryGetContext("region") ?? process.env.CDK_DEFAULT_REGION ?? "us-west-2",
};

// Custom domain + TLS certificate. REQUIRED, not optional.
//
// These used to be optional, and leaving them out deployed a public HTTP-only
// ALB: the login form, the submitted password and the session cookie all in
// cleartext on the internet. Rather than silently degrading, synthesis fails —
// so a deployment can no longer end up plaintext by omission.
//
//   cdk deploy --all \
//     --context domainName=example.com \
//     --context subdomain=xrpl-agentic-payments \
//     --context certificateArn=arn:aws:acm:...
const domainName = app.node.tryGetContext("domainName");
const subdomain = app.node.tryGetContext("subdomain") ?? "xrpl-agentic-payments";
const certificateArn = app.node.tryGetContext("certificateArn");

if (!domainName || !certificateArn) {
  throw new Error(
    [
      "TLS is mandatory: both `domainName` and `certificateArn` context values are required.",
      "",
      "  cdk deploy --all \\",
      "    --context domainName=example.com \\",
      "    --context subdomain=xrpl-agentic-payments \\",
      "    --context certificateArn=arn:aws:acm:us-west-2:<account>:certificate/<id>",
      "",
      `Received: domainName=${domainName ?? "<unset>"}, certificateArn=${certificateArn ?? "<unset>"}.`,
      "The certificate must cover <subdomain>.<domainName> and the apex domain must",
      "already be a Route 53 hosted zone in this account.",
    ].join("\n")
  );
}

const domain = { domainName, subdomain, certificateArn };

// XRPL addresses the web tier needs. PUBLIC values — an XRPL address is what you
// hand someone so they can pay you — which is the whole reason they come in as
// context alongside domainName rather than out of the wallets secret.
//
// The web tier only needs these two strings, and it used to get them by reading
// config/wallets.json, the file that also holds every SEED. That is why the EKS
// stack mounted the wallets secret into the web pod: two addresses were dragging
// the treasury and execution private keys into a long-lived multi-tenant process
// that cannot spend them and has no other use for them. Signing lives in the MCP
// server on AgentCore Runtime and in the Lambda tools, which read the seed
// themselves at invoke time. Passing the addresses here removes the mount.
//
// Read them out of the config/wallets.json that scripts/provision_wallets.py
// wrote (`jq -r '.execution.address, ._metadata.rlusd_issuer' config/wallets.json`)
// and pass them on the deploy command.
const executionAddress = app.node.tryGetContext("executionAddress");
const rlusdIssuer = app.node.tryGetContext("rlusdIssuer");

if (!executionAddress || !rlusdIssuer) {
  throw new Error(
    [
      "`executionAddress` and `rlusdIssuer` context values are required.",
      "",
      "  cdk deploy --all \\",
      "    --context executionAddress=r... \\",
      "    --context rlusdIssuer=r...",
      "",
      `Received: executionAddress=${executionAddress ?? "<unset>"}, ` +
        `rlusdIssuer=${rlusdIssuer ?? "<unset>"}.`,
      "",
      "Both are public XRPL addresses, not secrets. Get them from the wallets",
      "file scripts/provision_wallets.py wrote:",
      "",
      "  jq -r '.execution.address, ._metadata.rlusd_issuer' config/wallets.json",
      "",
      "This fails at synth on purpose: without them the web tier cannot resolve",
      "a source account, and the alternative — falling back to mounting the",
      "wallet SEEDS into the web pod to recover two public strings — is what",
      "this replaced.",
    ].join("\n")
  );
}

const xrplAddresses = { executionAddress, rlusdIssuer };

// Stack 1: Core infrastructure (IAM, ECR, Secrets)
const infraStack = new XrplAgenticPaymentsStack(app, "XrplAgenticPaymentsStack", {
  env: ENV,
  description: "XRPL Agentic Payments - Core infrastructure (IAM, ECR, Secrets, AgentCore)",
});

// Stack 2: Web frontend (legacy EC2 — kept for reference; EKS is the primary path)
const webStack = new XrplAgenticPaymentsWebStack(app, "XrplAgenticPaymentsWebStack", {
  env: ENV,
  description: "XRPL Agentic Payments - Web frontend (ALB + EC2)",
  domain,
});

// Stack 3: Lambda tools + the AgentCore Gateway that fronts them.
//
// Declared BEFORE the EKS stack because the EKS stack consumes the Gateway's URL
// and ARN: the Gateway is the pod's tool path, so this ordering is what makes
// `eksStack` depend on `toolsStack` rather than the two being unrelated.
const toolsStack = new XrplAgenticPaymentsToolsStack(app, "XrplAgenticPaymentsToolsStack", {
  env: ENV,
  description:
    "XRPL Agentic Payments - 8 XRPL tool Lambdas behind an AgentCore Gateway",
});

// Stack 4: EKS compute layer (ultra-performance, auto-scaling)
const eksStack = new XrplAgenticPaymentsEksStack(app, "XrplAgenticPaymentsEksV2", {
  env: ENV,
  description:
    "XRPL Agentic Payments - EKS cluster with Karpenter (Graviton3, HPA, Pod Identity)",
  domain: {
    hostname: `${domain.subdomain}.${domain.domainName}`,
    certificateArn: domain.certificateArn,
  },
  xrplAddresses,
  gateway: {
    url: toolsStack.gatewayUrl,
    id: toolsStack.gatewayId,
    arn: toolsStack.gatewayArn,
  },
});

// ═══════════════════════════════════════════════════════════════════════
// cdk-nag suppressions — documented, accepted risk posture for this
// PROTOTYPE (see cdk.Tags "Phase: prototype" on every stack above).
//
// Each suppression below states explicitly why the finding does not
// apply, or why it is an accepted trade-off for a demo/prototype rather
// than a production deployment. Anyone hardening this for production
// should treat every entry here as a checklist of what to fix first.
// ═══════════════════════════════════════════════════════════════════════

// --- XrplAgenticPaymentsStack (infra-stack.ts) ---------------------------------
NagSuppressions.addStackSuppressions(infraStack, [
  {
    id: "AwsSolutions-IAM5",
    reason:
      "Wildcard resources here are intentionally scoped to this account/region " +
      "(e.g. arn:aws:bedrock-agentcore:{region}:{account}:*) rather than a " +
      "specific runtime/session ID, because AgentCore payment sessions and " +
      "workload identities do not expose a stable resource ID at CDK synth " +
      "time — the resource is created at runtime, not deploy time. " +
      "ecr:GetAuthorizationToken is documented by AWS as requiring Resource:*. " +
      "See infra-stack.ts inline comments for per-statement justification.",
  },
  {
    id: "AwsSolutions-CB3",
    reason:
      "WebappImageBuild runs in privileged mode because it builds a container " +
      "image, which requires a Docker daemon inside the build container. " +
      "There is no non-privileged way to run `docker build` in CodeBuild. The " +
      "project's only source is an S3 zip this repo's own " +
      "deploy/build_webapp_image.sh uploads — no Git connection, no webhook, " +
      "so there is no path for third-party code to reach the privileged host.",
  },
  {
    id: "AwsSolutions-CB4",
    reason:
      "WebappImageBuild uses the default CodeBuild artifact encryption (an " +
      "AWS-managed key) rather than a customer-managed KMS key. The project " +
      "produces no artifacts — it pushes the built image straight to ECR, " +
      "which is encrypted at rest independently — so a CMK here would guard " +
      "an empty artifact path.",
  },
  {
    id: "AwsSolutions-S1",
    reason:
      "WebappBuildSource holds only the build-context zip that " +
      "deploy/build_webapp_image.sh uploads (application source already public " +
      "in this repo, with config/ excluded so no wallet seeds are in it). " +
      "S3 server access logging would need a second log bucket to record reads " +
      "of source code that carries no secrets, for a prototype build path.",
  },
  {
    id: "AwsSolutions-SMG4",
    reason:
      "XrplAgenticPaymentsCDPSecret is an empty placeholder shell populated manually " +
      "post-deploy via `aws secretsmanager put-secret-value` (Coinbase CDP " +
      "credentials, an external third-party API key not eligible for AWS " +
      "Secrets Manager automatic rotation). Rotation must be handled via " +
      "Coinbase's own key-rotation workflow, not AWS-native rotation.",
  },
]);

// --- XrplAgenticPaymentsWebStack (web-stack.ts) --------------------------------
NagSuppressions.addStackSuppressions(webStack, [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "AmazonSSMManagedInstanceCore and AmazonEC2ContainerRegistryReadOnly are " +
      "AWS-managed policies used for their intended purpose (SSM Session " +
      "Manager access instead of SSH, and ECR image pull). Re-authoring " +
      "these as customer-managed policies would duplicate AWS's own " +
      "maintained least-privilege scoping with no security benefit for a " +
      "prototype.",
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "EC2Role Bedrock/AgentCore resources are scoped to " +
      "foundation-model/inference-profile/runtime ARN patterns (see " +
      "web-stack.ts) — the trailing '*' is a required wildcard segment " +
      "within an otherwise account/region-scoped ARN, not an unscoped grant.",
  },
  {
    id: "AwsSolutions-VPC7",
    reason:
      "VPC Flow Logs are omitted for this prototype to avoid extra " +
      "CloudWatch Logs cost on a demo workload with no production traffic. " +
      "Enable via vpc.addFlowLog() before any production deployment.",
  },
  {
    id: "AwsSolutions-EC23",
    reason:
      "ALB-SG intentionally allows 0.0.0.0/0 on 443 because this is a " +
      "public-facing demo website with no VPN/allowlist requirement — the ALB " +
      "is the intended public entry point, not an internal service. Port 80 is " +
      "open only to serve the permanent redirect to HTTPS; no application " +
      "traffic is answered on it.",
  },
  {
    id: "AwsSolutions-EC26",
    reason:
      "ASG launch template does not set an explicit encrypted EBS volume; " +
      "Amazon Linux 2023 AMIs default to an unencrypted root volume unless " +
      "overridden. Accepted for this prototype (no sensitive data is stored " +
      "on the instance — credentials are fetched from Secrets Manager at " +
      "boot and never written to disk). Add a BlockDevice with " +
      "encrypted: true before production use.",
  },
  {
    id: "AwsSolutions-AS3",
    reason:
      "ASG scaling notifications (SNS on launch/terminate/error) are " +
      "omitted for this prototype; CloudWatch Container/EC2 Insights " +
      "already provide sufficient visibility for a demo. Add SNS " +
      "notifications for production on-call alerting.",
  },
  {
    id: "AwsSolutions-ELB2",
    reason:
      "ALB access logs to S3 are omitted for this prototype to avoid an " +
      "extra S3 bucket + lifecycle policy for a demo with no compliance " +
      "logging requirement. Enable via alb.logAccessLogs() before " +
      "production use.",
  },
  {
    id: "AwsSolutions-SMG4",
    reason:
      "WebAuthSecret stores the demo web UI's username/password, generated " +
      "randomly at deploy time and fetched by the EC2 instance at boot. " +
      "It is not a service/database credential Secrets Manager can rotate " +
      "automatically (there is no downstream service to notify of a " +
      "rotated value) — rotating it would require redeploying the ASG " +
      "instances to pick up the new value. Acceptable for a demo login.",
  },
]);

// --- XrplAgenticPaymentsEksV2 (eks-stack.ts) ------------------------------------
NagSuppressions.addStackSuppressions(eksStack, [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "AWS-managed policies (AmazonEKSWorkerNodePolicy, AmazonEKS_CNI_Policy, " +
      "AmazonEC2ContainerRegistryReadOnly, AmazonSSMManagedInstanceCore, " +
      "AmazonEKSClusterPolicy, AWSLambdaBasicExecutionRole, " +
      "AWSLambdaVPCAccessExecutionRole, AmazonElasticContainerRegistryPublicReadOnly, " +
      "AmazonEC2ContainerRegistryPullOnly) are used for their intended, " +
      "AWS-documented purpose on EKS node groups, the cluster service " +
      "role, and CDK's own generated Lambda custom-resource handlers " +
      "(ClusterResourceProvider, KubectlProvider) that this app does not " +
      "author or control.",
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "Wildcard resources fall into three categories, all accepted for this " +
      "prototype: (1) XrplAgenticPaymentsPodRole's Bedrock/AgentCore/Secrets Manager " +
      "resources are scoped to region/account/name-prefix ARN patterns — " +
      "see eks-stack.ts inline comments; (2) the AWS Load Balancer " +
      "Controller and Karpenter service-account roles need EC2/ELB/EKS " +
      "wildcard actions across dynamically-created resources (target " +
      "groups, security groups, EC2 instances) per AWS's own published " +
      "IAM policy for these controllers — iam:PassRole is separately " +
      "scoped to only the Karpenter node role; (3) CDK-generated Lambda " +
      "service roles for the EKS ClusterResourceProvider/KubectlProvider " +
      "custom resources, and the EKS cluster creation role's own scoped " +
      "eks:*/cluster and eks:*/fargateprofile ARNs, use CDK's own internal " +
      "wildcard grants, which this app does not author.",
  },
  {
    id: "AwsSolutions-EKS1",
    reason:
      "The EKS API server is deliberately PUBLIC_AND_PRIVATE for this " +
      "prototype so it can be administered with kubectl from a developer " +
      "laptop without a bastion host or VPN. Restrict to PRIVATE only " +
      "(or add an explicit CIDR allowlist) before production use.",
  },
  {
    id: "AwsSolutions-EC23",
    reason:
      "KarpenterNodeSG allows 0.0.0.0/0 ingress on the ports required for " +
      "node-to-node and control-plane communication in this single-cluster " +
      "prototype; the cluster is not multi-tenant. Tighten to VPC CIDR-only " +
      "ingress before production use.",
  },
  {
    id: "AwsSolutions-VPC7",
    reason:
      "VPC Flow Logs are omitted for this prototype to avoid extra " +
      "CloudWatch Logs cost on a demo workload with no production traffic. " +
      "Enable via vpc.addFlowLog() before any production deployment.",
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "The flagged Lambda function (@aws-cdk--aws-eks.KubectlProvider " +
      "Handler) is generated internally by the aws-cdk-lib EKS L2 " +
      "construct to run kubectl commands against the cluster; its runtime " +
      "version is pinned by the CDK library release, not by this app.",
  },
  {
    id: "AwsSolutions-SF1",
    reason:
      "The Step Functions waiter state machine belongs to CDK's internal " +
      "EKS ClusterResourceProvider custom resource (polls cluster creation " +
      "status) and is not authored or configurable by this app.",
  },
  {
    id: "AwsSolutions-SF2",
    reason:
      "Same CDK-internal EKS ClusterResourceProvider waiter state machine " +
      "as above — X-Ray tracing is not exposed as a configurable option on " +
      "this generated construct.",
  },
]);

// --- XrplAgenticPaymentsToolsStack (tools-stack.ts) -----------------------------
NagSuppressions.addStackSuppressions(toolsStack, [
  {
    id: "AwsSolutions-IAM4",
    reason:
      "AWSLambdaBasicExecutionRole (CloudWatch Logs write access) is the " +
      "AWS-managed policy explicitly designed for this exact purpose on " +
      "Lambda execution roles; re-authoring it as a customer-managed " +
      "policy would duplicate AWS's own least-privilege scoping.",
  },
  {
    id: "AwsSolutions-IAM5",
    reason:
      "The only wildcard IAM in this stack belongs to the CDK-generated " +
      "BucketDeployment handler (Custom::CDKBucketDeployment) that uploads " +
      "data/ofac_sdn.csv to the artifacts bucket: aws-cdk-lib grants that " +
      "handler s3:GetObject*/s3:List* on the CDK staging bucket and " +
      "s3:PutObject* under the destination prefix using its own internal " +
      "'<bucket>/*' grants, which this app does not author. The tool " +
      "Lambdas' own S3 grant is scoped to the single object key " +
      "data/ofac_sdn.csv, not a wildcard.",
  },
  {
    id: "AwsSolutions-L1",
    reason:
      "The flagged Lambda is aws-cdk-lib's generated BucketDeployment " +
      "(Custom::CDKBucketDeployment) handler; its Python runtime version is " +
      "pinned by the CDK library release, not by this app.",
  },
]);

// --- CDK-internal EKS constructs (ClusterResourceProvider, KubectlProvider) ---
// These are singleton custom-resource Lambdas + Step Functions that
// aws-cdk-lib's EKS L2 construct generates internally to call the EKS API
// (cluster create/update) and run kubectl. They live under XrplAgenticPaymentsEksV2
// but are created lazily during synthesis, after stack-level suppressions
// are registered, so they need their own path-based suppressions with
// `applyToChildren: true` to catch every nested resource under each path.
for (const providerPath of [
  "XrplAgenticPaymentsEksV2/@aws-cdk--aws-eks.ClusterResourceProvider",
  "XrplAgenticPaymentsEksV2/@aws-cdk--aws-eks.KubectlProvider",
]) {
  NagSuppressions.addResourceSuppressionsByPath(
    eksStack,
    providerPath,
    [
      {
        id: "AwsSolutions-IAM4",
        reason:
          "AWS-managed Lambda execution policies (AWSLambdaBasicExecutionRole, " +
          "AWSLambdaVPCAccessExecutionRole) on aws-cdk-lib's own generated " +
          "custom-resource handlers for EKS cluster/kubectl operations; not " +
          "authored or configurable by this app.",
      },
      {
        id: "AwsSolutions-IAM5",
        reason:
          "Wildcard IAM resources (e.g. Resource::<Handler.Arn>:*) on " +
          "aws-cdk-lib's own generated Lambda service roles and Step " +
          "Functions waiter state machine for the EKS custom resource " +
          "provider framework; not authored or configurable by this app.",
      },
      {
        id: "AwsSolutions-L1",
        reason:
          "Lambda runtime version on aws-cdk-lib's generated KubectlProvider " +
          "handler is pinned by the CDK library release, not by this app.",
      },
      {
        id: "AwsSolutions-SF1",
        reason:
          "CloudWatch Logs 'ALL' events logging on the CDK-internal " +
          "ClusterResourceProvider waiter state machine is not exposed as " +
          "a configurable option on this generated construct.",
      },
      {
        id: "AwsSolutions-SF2",
        reason:
          "X-Ray tracing on the CDK-internal ClusterResourceProvider " +
          "waiter state machine is not exposed as a configurable option " +
          "on this generated construct.",
      },
    ],
    true // applyToChildren
  );
}

// Run cdk-nag's AWS Solutions rule pack against every stack in the app.
// Findings are written to cdk.out/**/*NagReport.csv after `cdk synth`.
Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
