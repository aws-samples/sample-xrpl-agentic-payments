/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as apprunner from "aws-cdk-lib/aws-apprunner";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

/**
 * Tag the web app image is built and pulled as.
 *
 * eks-stack.ts references the image by URI string, not through the Repository
 * object, so nothing in CDK ties the two together — infra/test/infra.test.ts
 * asserts the names agree instead.
 */
export const WEBAPP_ECR_REPO_NAME = "xrpl-agentic-payments-webapp";

export class XrplAgenticPaymentsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ─────────────────────────────────────────────────────────────────
    // 1. IAM Role for AgentCore Payments
    // ─────────────────────────────────────────────────────────────────
    const paymentsRole = new iam.Role(this, "XrplAgenticPaymentsPaymentsRole", {
      roleName: "xrpl-agentic-payments-payments-role",
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("bedrock.amazonaws.com"),
        new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")
      ),
      description:
        "XRPL Agentic Payments - allows Bedrock AgentCore to manage payment sessions and process x402 micropayments",
    });

    // AgentCore Payments permissions
    paymentsRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentCorePayments",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:CreatePaymentSession",
          "bedrock-agentcore:CreatePaymentInstrument",
          "bedrock-agentcore:ProcessPayment",
          "bedrock-agentcore:GetPaymentSession",
          "bedrock-agentcore:ListPaymentSessions",
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:GetWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
        ],
        // Scoped to this account/region rather than "*" — AgentCore payment
        // sessions and workload identities do not expose a stable resource
        // ID at synth time, so account+region scoping is the tightest scope
        // achievable without a two-phase deploy. See infra-stack.ts IAM review.
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`,
        ],
      })
    );

    // ─────────────────────────────────────────────────────────────────
    // 2. IAM Role for Supervisor Agent (Bedrock Agent Runtime)
    // ─────────────────────────────────────────────────────────────────
    const agentRole = new iam.Role(this, "XrplAgenticPaymentsAgentRole", {
      roleName: "xrpl-agentic-payments-agent-role",
      assumedBy: new iam.CompositePrincipal(
        new iam.ServicePrincipal("bedrock.amazonaws.com"),
        new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")
      ),
      description:
        "XRPL Agentic Payments - allows the supervisor agent to invoke Bedrock models and call tools",
    });

    // Bedrock model invocation
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockModelInvocation",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        // Per AWS docs (bedrock cross-region inference IAM policy guide):
        // - foundation-model ARNs are never account-scoped (no account segment)
        //   and the region wildcard is required so the inference profile can
        //   route to whichever destination region it dispatches to.
        // - inference-profile ARNs ARE account-scoped; only the region should
        //   remain wildcarded to allow cross-region routing within this account.
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );

    // CloudWatch Logs — required for AgentCore Runtime container logging
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogs",
        effect: iam.Effect.ALLOW,
        actions: [
          "logs:CreateLogGroup",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        // Scoped to AgentCore runtime log groups only — the broader
        // log-group:* wildcard was removed (it granted write access to
        // every log group in the account, including unrelated apps).
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes/*`,
        ],
      })
    );

    // X-Ray — required for AgentCore observability
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "XRayTracing",
        effect: iam.Effect.ALLOW,
        actions: [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ],
        // X-Ray does not support resource-level ARN scoping for these
        // actions, so we restrict via a request-region condition instead
        // to prevent trace/telemetry writes to unrelated regions.
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:RequestedRegion": this.region },
        },
      })
    );

    // CloudWatch Metrics — required for AgentCore Runtime
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchMetrics",
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" },
        },
      })
    );

    // ECR — GetAuthorizationToken has no resource-level permissions and
    // must use "*" (this is an AWS-documented requirement, not an
    // over-grant). Image-pull actions are scoped to the specific
    // repository via ecrRepo.grantPull() below, once the repo exists.
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ECRAuthToken",
        effect: iam.Effect.ALLOW,
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      })
    );

    // AgentCore Identity — required for workload identity
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentCoreIdentity",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
          "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
          "bedrock-agentcore:InvokeAgentRuntime",
        ],
        // Scoped to this account/region — see AgentCorePayments comment above.
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`,
        ],
      })
    );

    // ─────────────────────────────────────────────────────────────────
    // 3. ECR Repository for AgentCore Runtime container
    // ─────────────────────────────────────────────────────────────────
    const ecrRepo = new ecr.Repository(this, "XrplAgenticPaymentsECR", {
      repositoryName: "xrpl-agentic-payments-mcp-server",
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        {
          maxImageCount: 5,
          description: "Keep only last 5 images",
        },
      ],
    });

    // Grant the agent role permission to pull images
    ecrRepo.grantPull(agentRole);

    // AgentCore Runtime role needs ECR pull + additional permissions
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentCoreRuntimePermissions",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:CreateWorkloadIdentity",
          "bedrock-agentcore:GetWorkloadIdentity",
          "bedrock-agentcore:DeleteWorkloadIdentity",
          "bedrock-agentcore:CreateAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:ListAgentRuntimes",
          "bedrock-agentcore:CreateAgentRuntimeEndpoint",
          "bedrock-agentcore:GetAgentRuntimeEndpoint",
          "bedrock-agentcore:CreateGateway",
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:CreateGatewayTarget",
          "bedrock-agentcore:GetGatewayTarget",
        ],
        resources: ["*"],
      })
    );

    // ─────────────────────────────────────────────────────────────────
    // 4. Secrets Manager — placeholder for CDP credentials
    // ─────────────────────────────────────────────────────────────────
    // IMPORTANT: Never read local credential files at CDK synth time and
    // never pass real secret values into SecretValue.unsafePlainText() —
    // both approaches embed the plaintext secret directly into the
    // synthesized CloudFormation template (and therefore into any state
    // file, CI log, or version-controlled cdk.out/ directory).
    //
    // This creates an empty secret shell. Populate the real values after
    // deploy with:
    //   aws secretsmanager put-secret-value \
    //     --secret-id xrpl-agentic-payments/coinbase-cdp \
    //     --secret-string '{"apiKeyId":"...","apiKeySecret":"...","walletSecret":"..."}'
    const cdpSecret = new secretsmanager.Secret(this, "XrplAgenticPaymentsCDPSecret", {
      secretName: "xrpl-agentic-payments/coinbase-cdp",
      description:
        "Coinbase CDP API credentials for XRPL Agentic Payments x402 payments. " +
        "Populate with `aws secretsmanager put-secret-value` after deploy — see README.",
      secretStringValue: cdk.SecretValue.unsafePlainText(
        JSON.stringify({ apiKeyId: "", apiKeySecret: "", walletSecret: "" })
      ),
    });

    // Grant agent role access to the secret
    cdpSecret.grantRead(agentRole);
    cdpSecret.grantRead(paymentsRole);

    // XRPL wallet seeds. Created outside CDK by scripts/provision_wallets.py
    // (the same secret functions/shared.py reads), referenced by name so this
    // stack does not own — and cannot log — its value.
    //
    // The grant exists because wallet seeds are no longer bundled into the
    // AgentCore Runtime archive (deploy/package.sh used to copy
    // config/wallets.json into the zip, publishing private seeds to S3). Reading
    // this secret at startup is the replacement path for the runtime, exactly as
    // the Gateway Lambdas already do.
    const walletsSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "WalletsSecret",
      "xrpl-agentic-payments/wallets"
    );
    walletsSecret.grantRead(agentRole);

    // ─────────────────────────────────────────────────────────────────
    // 4. Web App ECR + App Runner Roles
    // ─────────────────────────────────────────────────────────────────
    const webappRepo = new ecr.Repository(this, "XrplAgenticPaymentsWebAppECR", {
      repositoryName: WEBAPP_ECR_REPO_NAME,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 3 }],
    });

    // ─────────────────────────────────────────────────────────────────
    // 4b. CodeBuild: build the web app image for arm64
    // ─────────────────────────────────────────────────────────────────
    // The EKS nodes are Graviton, so this image must be linux/arm64 — the
    // Karpenter NodePool in eks-stack.ts requires "arm64" outright, and an
    // amd64 image lands the pod in CrashLoopBackOff with an exec format error.
    //
    // CDK does not build it. eks-stack.ts names the tag as a plain string, so
    // something has to put an image in the repository before the Deployment can
    // schedule at all. This project is that something, so the answer is not
    // "a laptop with Docker installed": it runs ON arm64 (LinuxArmBuildImage),
    // so the build is native rather than QEMU-emulated, and needs no local
    // container runtime.
    //
    // Source is an S3 zip rather than a Git connection so there is nothing to
    // authorize and no webhook: deploy/build_webapp_image.sh assembles the zip
    // from an explicit file list, uploads it, and starts the build.
    const buildSourceBucket = new s3.Bucket(this, "WebappBuildSource", {
      bucketName: `xrpl-agentic-payments-build-src-${this.account}-${this.region}`,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // No autoDeleteObjects: that would add a custom-resource Lambda (and its
      // own IAM wildcards) to carry a build cache. Empty it by hand before
      // `cdk destroy` if you ever tear this down.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const webappBuild = new codebuild.Project(this, "WebappImageBuild", {
      projectName: "xrpl-agentic-payments-webapp-build",
      description:
        "Builds Dockerfile.webapp for linux/arm64 and pushes it to ECR for the EKS Deployment",
      source: codebuild.Source.s3({
        bucket: buildSourceBucket,
        path: "webapp-src.zip",
      }),
      environment: {
        // arm64 build host — this is what makes the resulting image arm64.
        buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
        computeType: codebuild.ComputeType.SMALL,
        // Required to run a Docker daemon inside the build container.
        privileged: true,
      },
      environmentVariables: {
        ECR_REPO: { value: webappRepo.repositoryUri },
        IMAGE_TAG: { value: "latest" },
      },
      timeout: cdk.Duration.minutes(30),
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: {
            commands: [
              // Log in to the REGISTRY host, not the repository path — ECR_REPO
              // carries the /<repo> suffix and `docker login` rejects it.
              'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | ' +
                'docker login --username AWS --password-stdin "${ECR_REPO%%/*}"',
              // A zip that carries wallet seeds would bake them into an image
              // layer, where `docker history` keeps them even if a later layer
              // deletes the file. build_webapp_image.sh excludes config/; this
              // is the second check, on the build host, in case the zip was
              // assembled some other way.
              'if [ -e config/wallets.json ]; then echo "refusing to build: config/wallets.json is in the build context" >&2; exit 1; fi',
            ],
          },
          build: {
            commands: [
              'docker build --platform linux/arm64 -f Dockerfile.webapp -t "$ECR_REPO:$IMAGE_TAG" .',
            ],
          },
          post_build: {
            commands: [
              'docker push "$ECR_REPO:$IMAGE_TAG"',
              'docker image inspect "$ECR_REPO:$IMAGE_TAG" --format "built {{.Architecture}} for {{.Os}}"',
            ],
          },
        },
      }),
    });

    webappRepo.grantPullPush(webappBuild);

    const appRunnerAccessRole = new iam.Role(this, "AppRunnerAccessRole", {
      roleName: "xrpl-agentic-payments-apprunner-access",
      assumedBy: new iam.ServicePrincipal("build.apprunner.amazonaws.com"),
    });
    webappRepo.grantPull(appRunnerAccessRole);

    const appRunnerInstanceRole = new iam.Role(this, "AppRunnerInstanceRole", {
      roleName: "xrpl-agentic-payments-apprunner-instance",
      assumedBy: new iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
    });
    appRunnerInstanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        // Same scoping pattern as agentRole's BedrockModelInvocation statement.
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );
    appRunnerInstanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/*`,
        ],
      })
    );
    appRunnerInstanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:GetAgentRuntime",
          "cloudwatch:GetMetricStatistics",
          "logs:DescribeLogGroups",
          "logs:GetLogEvents",
        ],
        // These read-only observability actions do not support resource-level
        // scoping to a specific AgentCore runtime/log group, so we restrict
        // via a request-region condition instead of a bare "*".
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:RequestedRegion": this.region },
        },
      })
    );
    cdpSecret.grantRead(appRunnerInstanceRole);

    // ─────────────────────────────────────────────────────────────────
    // 5. Outputs
    // ─────────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "PaymentsRoleArn", {
      value: paymentsRole.roleArn,
      description: "IAM Role ARN for AgentCore Payments",
    });

    new cdk.CfnOutput(this, "AgentRoleArn", {
      value: agentRole.roleArn,
      description: "IAM Role ARN for Supervisor Agent",
    });

    new cdk.CfnOutput(this, "CDPSecretArn", {
      value: cdpSecret.secretArn,
      description: "Secrets Manager ARN for Coinbase CDP credentials",
    });

    new cdk.CfnOutput(this, "ECRRepoUri", {
      value: ecrRepo.repositoryUri,
      description: "ECR Repository URI for MCP server container",
    });

    new cdk.CfnOutput(this, "WebAppECRUri", {
      value: webappRepo.repositoryUri,
      description: "ECR Repository URI for web app container",
    });

    new cdk.CfnOutput(this, "WebappBuildSourceBucket", {
      value: buildSourceBucket.bucketName,
      description: "S3 bucket deploy/build_webapp_image.sh uploads webapp-src.zip to",
    });

    new cdk.CfnOutput(this, "WebappBuildProject", {
      value: webappBuild.projectName,
      description: "CodeBuild project that builds the arm64 web app image",
    });

    new cdk.CfnOutput(this, "AppRunnerAccessRoleArn", {
      value: appRunnerAccessRole.roleArn,
      description: "App Runner ECR access role",
    });

    new cdk.CfnOutput(this, "AppRunnerInstanceRoleArn", {
      value: appRunnerInstanceRole.roleArn,
      description: "App Runner instance role",
    });

    // ─────────────────────────────────────────────────────────────────
    // 5. Cost Allocation Tags (applied to ALL resources in stack)
    // ─────────────────────────────────────────────────────────────────
    cdk.Tags.of(this).add("Project", "XrplAgenticPayments");
    cdk.Tags.of(this).add("Environment", "development");
    cdk.Tags.of(this).add("ManagedBy", "cdk");
    cdk.Tags.of(this).add("Phase", "prototype");
  }
}
