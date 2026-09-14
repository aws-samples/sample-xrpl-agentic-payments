/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as eks from "aws-cdk-lib/aws-eks";
import * as iam from "aws-cdk-lib/aws-iam";
import { Asset } from "aws-cdk-lib/aws-s3-assets";
import * as path from "path";
import { Construct } from "constructs";
import { KubectlV31Layer } from "@aws-cdk/lambda-layer-kubectl-v31";
import { MCP_PROTOCOL_VERSION } from "./tools-stack";

/**
 * XRPL Agentic Payments EKS Stack — Ultra-Performance Compute Layer
 *
 * Architecture:
 * - EKS 1.31 cluster with Graviton3 (c7g) nodes via Karpenter
 * - VPC with public (ALB) + private (pods) subnets, NAT Gateway
 * - VPC CNI with prefix delegation for high pod density
 * - EKS Pod Identity for Bedrock + Secrets Manager access
 * - AWS Load Balancer Controller for ALB Ingress with WebSocket stickiness
 * - HPA scaling on CPU + KEDA on active connections for Swell burst
 * - Container Insights + ADOT for full observability
 *
 * Well-Architected alignment:
 * - PERF01: Right-sized Graviton3 compute-optimized instances
 * - PERF04: Topology-aware routing, prefix delegation
 * - REL06: Multi-AZ, HPA, PDB, health checks
 * - SEC02: Pod Identity (least privilege), network policies
 * - SEC09: TLS 1.3, private subnets, encrypted EBS
 * - COST03: Karpenter consolidation, Spot for burst
 */
/**
 * Custom hostname + TLS cert for the ALB Ingress. REQUIRED.
 *
 * Omitting this used to produce an Ingress with `listen-ports: [{"HTTP":80}]`
 * and no redirect — i.e. the login form and its session cookie travelling in
 * cleartext over the public internet. There is no HTTP-only path any more;
 * synthesis fails instead of quietly downgrading.
 */
export interface EksDomainConfig {
  /** Full hostname to route, e.g. "xrpl-agentic-payments.example.com" */
  hostname: string;
  /** ARN of an existing ACM certificate covering `hostname` */
  certificateArn: string;
}

export interface XrplAgenticPaymentsEksStackProps extends cdk.StackProps {
  /** Custom hostname + ACM certificate. Required — see EksDomainConfig. */
  domain: EksDomainConfig;
  /** Public XRPL addresses the web tier needs. Required — see XrplAddresses. */
  xrplAddresses: XrplAddresses;
  /** The AgentCore Gateway the pod calls tools through. Required — see GatewayRef. */
  gateway: GatewayRef;
}

/**
 * Where the pod's tool calls go, and what to scope its IAM grant to.
 *
 * Supplied by XrplAgenticPaymentsToolsStack, which owns the Gateway. Required
 * rather than optional because the Gateway IS the tool path: with no URL the
 * orchestrator has nothing to call, and the previous arrangement — a pod
 * permitted to reach `gateway/*` with no gateway existing anywhere — is what
 * left the eight tool Lambdas deployed and unreachable.
 */
export interface GatewayRef {
  /** MCP endpoint, e.g. `https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`. */
  url: string;
  /** Gateway identifier, shown in the web UI's infrastructure panel. */
  id: string;
  /** Gateway ARN, used to scope bedrock-agentcore:InvokeGateway. */
  arn: string;
}

/**
 * The two public XRPL addresses the web tier resolves at run time.
 *
 * Public, and deliberately so: these are addresses, not seeds. The web tier
 * needs a source account to quote and an issuer to price RLUSD against, and it
 * used to obtain both by reading config/wallets.json — the file that also
 * contains every private seed. Mounting that secret into a long-lived
 * multi-tenant pod to recover two public strings put the treasury and execution
 * keys inside the process that handles every user's request, for no benefit:
 * nothing in the web tier signs. Signing is in the MCP server on AgentCore
 * Runtime and in the Lambda tools, each reading the seed itself at invoke time.
 *
 * Passed as plain container env vars (XRPL_EXECUTION_ADDRESS /
 * XRPL_RLUSD_ISSUER), which src/agents/orchestrator.py prefers over the wallets
 * file. With these set, the pod needs no wallet mount at all.
 */
export interface XrplAddresses {
  /** Address the Execution Agent pays from (`.execution.address`). */
  executionAddress: string;
  /** Testnet RLUSD issuer (`._metadata.rlusd_issuer`). */
  rlusdIssuer: string;
}

/**
 * EKS cluster name. Also used as the Karpenter discovery tag value below —
 * Karpenter's own documentation uses the cluster name for
 * `karpenter.sh/discovery`, and keeping them identical means one cluster's
 * NodePools can never accidentally claim another cluster's subnets.
 */
export const CLUSTER_NAME = "xrpl-agentic-payments-v2";

/** Tag key Karpenter uses to discover subnets and security groups. */
export const KARPENTER_DISCOVERY_TAG = "karpenter.sh/discovery";

/**
 * Value of `karpenter.sh/discovery` on the subnets and node security group,
 * and in the EC2NodeClass subnet/securityGroup selectors. These MUST be the
 * same string in all five places: if they diverge, Karpenter resolves 0
 * subnets and 0 security groups, no NodeClaim ever launches, and every pod
 * that does not fit on the tainted system node group stays Pending forever
 * (silent autoscaling failure — `cdk deploy` still succeeds).
 */
export const KARPENTER_DISCOVERY_VALUE = CLUSTER_NAME;

/**
 * Secrets Manager secret holding the web UI username/password. Created (and
 * randomly generated) by web-stack.ts — referenced here by name so the two
 * stacks stay independently deployable.
 */
const WEB_AUTH_SECRET_NAME = "xrpl-agentic-payments/web-auth";

/**
 * Kubernetes Secret the web Deployment reads via `secretKeyRef`. It is not
 * authored as a manifest — the Secrets Store CSI driver materialises it from
 * WEB_AUTH_SECRET_NAME (see section 8b).
 */
export const WEB_AUTH_K8S_SECRET = "xrpl-agentic-payments-auth";

/** SecretProviderClass that tells the AWS provider what to fetch and sync. */
const WEB_AUTH_SPC_NAME = "xrpl-agentic-payments-web-auth";

/**
 * The wallet seeds (Secrets Manager: xrpl-agentic-payments/wallets) are NOT
 * mounted into the web pod and there is no constant for them here on purpose.
 *
 * They used to be, at /app/config/wallets.json, because the orchestrator read
 * that file to get the execution and RLUSD issuer addresses. Those two values
 * are public and now arrive as env vars (see XrplAddresses), so the pod has no
 * reason to hold private keys it cannot use. Signing happens in the Lambda tools
 * (functions/shared.py) and in the MCP server on AgentCore Runtime, which read
 * the secret directly at invoke time.
 *
 * Dockerfile.webapp does not copy config/ either, so the web image contains no
 * seed by any route.
 */

export class XrplAgenticPaymentsEksStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: XrplAgenticPaymentsEksStackProps) {
    super(scope, id, props);

    // Fail at synth rather than deploying a public plaintext login page. Also
    // checked in bin/infra.ts; repeated here because the stack is exported and
    // can be instantiated directly.
    const domain = props?.domain;
    if (!domain?.hostname || !domain?.certificateArn) {
      throw new Error(
        "XrplAgenticPaymentsEksStack requires TLS: pass domain.hostname and " +
          "domain.certificateArn (cdk deploy --context domainName=example.com " +
          "--context certificateArn=arn:aws:acm:...). Serving the login form " +
          "over plaintext HTTP is not a supported configuration."
      );
    }

    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;
    const imageTag = this.node.tryGetContext("webImageTag") ?? "latest";

    // ═══════════════════════════════════════════════════════════════════
    // 1. VPC — Public (ALB) + Private (Pods) + Isolated (future DB)
    // ═══════════════════════════════════════════════════════════════════
    const vpc = new ec2.Vpc(this, "EksVpc", {
      maxAzs: 2,
      natGateways: 1, // Single NAT for cost (upgrade to 2 for HA in prod)
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 22, // /22 = 1024 IPs per AZ for pods (prefix delegation)
          name: "Private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
    });

    // Tag subnets for Karpenter discovery and ALB controller
    vpc.publicSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add("kubernetes.io/role/elb", "1");
      cdk.Tags.of(subnet).add(KARPENTER_DISCOVERY_TAG, KARPENTER_DISCOVERY_VALUE);
      cdk.Tags.of(subnet).add("network", "public");
    });
    vpc.privateSubnets.forEach((subnet) => {
      cdk.Tags.of(subnet).add("kubernetes.io/role/internal-elb", "1");
      cdk.Tags.of(subnet).add(KARPENTER_DISCOVERY_TAG, KARPENTER_DISCOVERY_VALUE);
      cdk.Tags.of(subnet).add("network", "private");
    });

    // ═══════════════════════════════════════════════════════════════════
    // 2. EKS Cluster
    // ═══════════════════════════════════════════════════════════════════
    const cluster = new eks.Cluster(this, "XrplAgenticPaymentsCluster", {
      clusterName: CLUSTER_NAME,
      version: eks.KubernetesVersion.V1_31,
      kubectlLayer: new KubectlV31Layer(this, "KubectlLayer"),
      vpc,
      vpcSubnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
      defaultCapacity: 0,
      endpointAccess: eks.EndpointAccess.PUBLIC_AND_PRIVATE,
      clusterLogging: [
        eks.ClusterLoggingTypes.API,
        eks.ClusterLoggingTypes.AUDIT,
        eks.ClusterLoggingTypes.AUTHENTICATOR,
        eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
        eks.ClusterLoggingTypes.SCHEDULER,
      ],
    });

    // ═══════════════════════════════════════════════════════════════════
    // 3. System Node Group (CoreDNS, Karpenter controller, LB controller)
    // ═══════════════════════════════════════════════════════════════════
    const systemNg = cluster.addNodegroupCapacity("SystemNodes", {
      instanceTypes: [new ec2.InstanceType("t4g.medium")],
      minSize: 2,
      maxSize: 3,
      desiredSize: 2,
      amiType: eks.NodegroupAmiType.AL2023_ARM_64_STANDARD,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      labels: { "node-role": "system" },
      taints: [
        {
          key: "CriticalAddonsOnly",
          value: "true",
          effect: eks.TaintEffect.PREFER_NO_SCHEDULE,
        },
      ],
      diskSize: 30,
    });

    // ═══════════════════════════════════════════════════════════════════
    // 4. Karpenter Node Role (for workload nodes it provisions)
    // ═══════════════════════════════════════════════════════════════════
    const karpenterNodeRole = new iam.Role(this, "KarpenterNodeRole", {
      roleName: "KarpenterNodeRole-xrpl-agentic-payments",
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonEKSWorkerNodePolicy"),
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonEKS_CNI_Policy"),
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonEC2ContainerRegistryReadOnly"
        ),
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonSSMManagedInstanceCore"
        ),
      ],
    });

    // Map Karpenter node role to aws-auth
    cluster.awsAuth.addRoleMapping(karpenterNodeRole, {
      groups: ["system:bootstrappers", "system:nodes"],
      username: "system:node:{{EC2PrivateDNSName}}",
    });

    // ═══════════════════════════════════════════════════════════════════
    // 5. Pod IAM Role (EKS Pod Identity for workload pods)
    // ═══════════════════════════════════════════════════════════════════
    const podRole = new iam.Role(this, "XrplAgenticPaymentsPodRole", {
      roleName: "xrpl-agentic-payments-pod-role",
      assumedBy: new iam.ServicePrincipal("pods.eks.amazonaws.com"),
      description:
        "EKS Pod Identity role for XRPL Agentic Payments pods - Bedrock, AgentCore, Secrets, CloudWatch",
    });

    // Allow sts:TagSession (required for Pod Identity)
    podRole.assumeRolePolicy?.addStatements(
      new iam.PolicyStatement({
        actions: ["sts:TagSession"],
        principals: [new iam.ServicePrincipal("pods.eks.amazonaws.com")],
      })
    );

    // Bedrock model invocation
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "BedrockInvocation",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          `arn:aws:bedrock:${region}::foundation-model/anthropic.*`,
          `arn:aws:bedrock:${region}:*:inference-profile/*`,
        ],
      })
    );

    // AgentCore Gateway — the tool path. InvokeGateway is the action that
    // authorizes the SigV4-signed POST to the gateway's /mcp endpoint, and it is
    // scoped to the one gateway this deployment creates rather than `gateway/*`.
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentCoreGateway",
        actions: ["bedrock-agentcore:InvokeGateway"],
        resources: [props.gateway.arn],
      })
    );

    // AgentCore Runtime. Still granted, but it is no longer the tool path: the
    // orchestrator reaches tools through the Gateway above. This covers the
    // Runtime-hosted MCP server, which remains deployed and directly invocable
    // (deploy/main.py) for local and one-off use.
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AgentCoreRuntime",
        actions: [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore:ListAgentRuntimes",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${region}:${this.account}:runtime/*`,
        ],
      })
    );

    // Secrets Manager (Coinbase CDP + web auth)
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SecretsAccess",
        // DescribeSecret is required by the AWS Secrets and Configuration
        // Provider (section 8b) in addition to GetSecretValue.
        actions: ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
        resources: [
          `arn:aws:secretsmanager:${region}:${this.account}:secret:xrpl-agentic-payments/*`,
        ],
      })
    );

    // CloudWatch metrics + X-Ray — these AWS services do not support
    // resource-level ARN scoping for these specific actions, so we restrict
    // via a request-region condition instead of a bare "*".
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "Observability",
        actions: [
          "cloudwatch:PutMetricData",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics",
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
        ],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:RequestedRegion": region },
        },
      })
    );

    // CloudWatch Logs — scoped to the XRPL Agentic Payments application log group only.
    // logs:DescribeLogGroups/StartQuery/GetQueryResults are query-plane
    // actions without meaningful resource scoping, but the write actions
    // (CreateLogGroup/CreateLogStream/PutLogEvents) are restricted to this
    // pod's own log group prefix, not every log group in the account.
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsWrite",
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [
          `arn:aws:logs:${region}:${account}:log-group:/aws/eks/${CLUSTER_NAME}/*`,
        ],
      })
    );
    podRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CloudWatchLogsQuery",
        actions: [
          "logs:DescribeLogGroups",
          "logs:StartQuery",
          "logs:GetQueryResults",
        ],
        resources: ["*"],
      })
    );

    // ═══════════════════════════════════════════════════════════════════
    // 6. AWS Load Balancer Controller (via Helm)
    // ═══════════════════════════════════════════════════════════════════
    const lbControllerSa = cluster.addServiceAccount("LBControllerSA", {
      name: "aws-load-balancer-controller",
      namespace: "kube-system",
    });

    lbControllerSa.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        // Explicit actions per the AWS Load Balancer Controller's documented
        // IAM policy (replaces ec2:Describe*, elasticloadbalancing:*,
        // waf-regional:*, wafv2:*, shield:* wildcards). WAF/Shield actions are
        // omitted since this controller is not configured to manage them —
        // add them back only if WAF/Shield integration is enabled.
        actions: [
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeVpcs",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeTags",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:DescribeListenerCertificates",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetHealth",
          "elasticloadbalancing:DescribeTags",
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:CreateRule",
          "elasticloadbalancing:DeleteRule",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:RegisterTargets",
          "elasticloadbalancing:DeregisterTargets",
          "elasticloadbalancing:SetSubnets",
          "elasticloadbalancing:SetSecurityGroups",
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags",
          "cognito-idp:DescribeUserPoolClient",
          "acm:ListCertificates",
          "acm:DescribeCertificate",
          "tag:GetResources",
          "tag:TagResources",
        ],
        resources: ["*"],
      })
    );

    // iam:CreateServiceLinkedRole for the ELB service-linked role is a
    // one-time, account-level action scoped to the specific role that AWS
    // creates on first use — keep it in its own statement, scoped by ARN
    // pattern rather than granting it alongside broad ELB actions above.
    lbControllerSa.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["iam:CreateServiceLinkedRole"],
        resources: [
          `arn:aws:iam::${this.account}:role/aws-service-role/elasticloadbalancing.amazonaws.com/AWSServiceRoleForElasticLoadBalancing`,
        ],
        conditions: {
          StringEquals: {
            "iam:AWSServiceName": "elasticloadbalancing.amazonaws.com",
          },
        },
      })
    );

    // The chart is vendored under infra/charts/aws-load-balancer-controller
    // (an unmodified copy of aws-load-balancer-controller 3.5.0 from
    // https://aws.github.io/eks-charts) and shipped as a CDK S3 asset instead
    // of being pulled from that Helm repository at deploy time. Two reasons:
    //
    //   * aws.github.io is a github.io domain, not an AWS/Amazon one. Fetching
    //     third-party software from an external resource during deployment is
    //     not allowed; hosting the chart in S3 is the sanctioned alternative,
    //     and the CDK assets bucket in this account is exactly that.
    //   * The old call pinned no `version`, so every deployment silently took
    //     whatever was newest in the repo. A vendored chart is the pin.
    //
    // The controller image itself already comes from ECR Public
    // (public.ecr.aws/eks/aws-load-balancer-controller, see the chart's
    // values.yaml), so nothing in this install path reaches a non-AWS registry.
    //
    // `chartAsset` is mutually exclusive with `repository` and `version` —
    // the version lives in the vendored Chart.yaml. To upgrade, replace the
    // directory contents with a newer chart release.
    const lbChartAsset = new Asset(this, "AWSLoadBalancerControllerChart", {
      path: path.join(__dirname, "..", "charts", "aws-load-balancer-controller"),
    });

    const lbChart = cluster.addHelmChart("AWSLoadBalancerController", {
      chartAsset: lbChartAsset,
      namespace: "kube-system",
      values: {
        clusterName: CLUSTER_NAME,
        serviceAccount: {
          create: false,
          name: "aws-load-balancer-controller",
        },
        region: region,
        vpcId: vpc.vpcId,
        enablePodReadinessGateInject: true,
      },
    });

    // ═══════════════════════════════════════════════════════════════════
    // 7. Karpenter (via Helm)
    // ═══════════════════════════════════════════════════════════════════
    const karpenterNs = cluster.addManifest("KarpenterNamespace", {
      apiVersion: "v1",
      kind: "Namespace",
      metadata: { name: "karpenter" },
    });

    const karpenterSa = cluster.addServiceAccount("KarpenterSA", {
      name: "karpenter",
      namespace: "karpenter",
    });
    karpenterSa.node.addDependency(karpenterNs);

    // Karpenter controller IAM permissions
    karpenterSa.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "ec2:CreateFleet",
          "ec2:CreateLaunchTemplate",
          "ec2:CreateTags",
          "ec2:DeleteLaunchTemplate",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeImages",
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplates",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSpotPriceHistory",
          "ec2:DescribeSubnets",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "pricing:GetProducts",
          "ssm:GetParameter",
          "eks:DescribeCluster",
        ],
        resources: ["*"],
      })
    );

    // iam:PassRole scoped to only the Karpenter node role — a wildcard here
    // would let a compromised Karpenter controller pass ANY role in the
    // account to the EC2 instances it launches (privilege escalation).
    karpenterSa.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [karpenterNodeRole.roleArn],
      })
    );

    const karpenterChart = cluster.addHelmChart("Karpenter", {
      chart: "oci://public.ecr.aws/karpenter/karpenter",
      version: "1.3.0",
      namespace: "karpenter",
      values: {
        settings: {
          clusterName: CLUSTER_NAME,
          clusterEndpoint: cluster.clusterEndpoint,
        },
        serviceAccount: {
          create: false,
          name: "karpenter",
        },
        tolerations: [
          { key: "CriticalAddonsOnly", operator: "Exists" },
        ],
        nodeSelector: { "node-role": "system" },
      },
    });
    karpenterChart.node.addDependency(karpenterSa);

    // ═══════════════════════════════════════════════════════════════════
    // 8. Karpenter NodePool + EC2NodeClass (Graviton3 c7g)
    // ═══════════════════════════════════════════════════════════════════
    const karpenterNodePool = cluster.addManifest("KarpenterNodePool", {
      apiVersion: "karpenter.sh/v1",
      kind: "NodePool",
      metadata: { name: "xrpl-agentic-payments" },
      spec: {
        disruption: {
          consolidateAfter: "60s",
          consolidationPolicy: "WhenEmptyOrUnderutilized",
        },
        limits: {
          cpu: "64",
          memory: "128Gi",
        },
        template: {
          metadata: {
            labels: { workload: "xrpl-agentic-payments", team: "isv-das" },
          },
          spec: {
            nodeClassRef: {
              group: "karpenter.k8s.aws",
              kind: "EC2NodeClass",
              name: "xrpl-agentic-payments",
            },
            expireAfter: "168h", // 7 days — forces node refresh for patches
            requirements: [
              {
                key: "kubernetes.io/arch",
                operator: "In",
                values: ["arm64"],
              },
              {
                key: "karpenter.sh/capacity-type",
                operator: "In",
                values: ["on-demand", "spot"],
              },
              {
                key: "node.kubernetes.io/instance-type",
                operator: "In",
                values: [
                  "c7g.large",
                  "c7g.xlarge",
                  "c7g.2xlarge",
                  "m7g.large",
                  "m7g.xlarge",
                ],
              },
              {
                key: "topology.kubernetes.io/zone",
                operator: "In",
                values: vpc.availabilityZones,
              },
            ],
          },
        },
      },
    });
    karpenterNodePool.node.addDependency(karpenterChart);

    const karpenterNodeClass = cluster.addManifest("KarpenterEC2NodeClass", {
      apiVersion: "karpenter.k8s.aws/v1",
      kind: "EC2NodeClass",
      metadata: { name: "xrpl-agentic-payments" },
      spec: {
        role: "KarpenterNodeRole-xrpl-agentic-payments",
        amiSelectorTerms: [{ alias: "al2023@latest" }],
        subnetSelectorTerms: [
          {
            tags: {
              [KARPENTER_DISCOVERY_TAG]: KARPENTER_DISCOVERY_VALUE,
              network: "private",
            },
          },
        ],
        securityGroupSelectorTerms: [
          { tags: { [KARPENTER_DISCOVERY_TAG]: KARPENTER_DISCOVERY_VALUE } },
        ],
        blockDeviceMappings: [
          {
            deviceName: "/dev/xvda",
            ebs: {
              volumeSize: "30Gi",
              volumeType: "gp3",
              encrypted: true,
            },
          },
        ],
        tags: {
          Project: "XrplAgenticPayments",
          ManagedBy: "karpenter",
        },
      },
    });
    karpenterNodeClass.node.addDependency(karpenterChart);

    // ═══════════════════════════════════════════════════════════════════
    // 8b. Secrets Store CSI driver + AWS Secrets and Configuration Provider
    // ═══════════════════════════════════════════════════════════════════
    // The web Deployment (section 10) reads its basic-auth credentials from
    // the Kubernetes Secret WEB_AUTH_K8S_SECRET. That Secret is deliberately
    // NOT written as a `cluster.addManifest` Secret:
    //
    //   * A literal value would require the password at synth time and would
    //     land in plaintext in the CloudFormation template and in cdk.out.
    //   * A `cdk.SecretValue.secretsManager()` dynamic reference does not
    //     work either: CloudFormation explicitly does not resolve
    //     `{{resolve:secretsmanager:...}}` inside custom resource properties,
    //     and every addManifest/addHelmChart is a custom resource
    //     (Custom::AWSCDK-EKS-KubernetesResource). The pod would receive the
    //     literal "{{resolve:...}}" string as its password.
    //
    // Instead the AWS Secrets and Configuration Provider (ASCP) reads
    // WEB_AUTH_SECRET_NAME from Secrets Manager at pod-mount time using the
    // pod's own IAM identity, and the Secrets Store CSI driver syncs the two
    // JSON keys into WEB_AUTH_K8S_SECRET. The secret value never enters the
    // CloudFormation template, cdk.out, or this repository.
    //
    // This is one dependency, not two: the ASCP ships the upstream
    // secrets-store-csi-driver alongside its own provider DaemonSet, and takes
    // the driver's Helm values under the "secrets-store-csi-driver" key.
    //
    // Installed as an EKS *managed add-on* rather than `addHelmChart`. The
    // chart lives at https://aws.github.io/secrets-store-csi-driver-provider-aws,
    // a github.io domain — pulling third-party software from an external
    // resource at deploy time is not allowed. The add-on is the same software
    // packaged and served by EKS itself, so there is no external fetch and no
    // chart to vendor. (`aws eks describe-addon-configuration` documents the
    // schema; EKS validates the provider half of it and passes the
    // "secrets-store-csi-driver" object through to the bundled driver.)
    //
    // CloudFormation holds the resource until the add-on reports ACTIVE, which
    // is what the old chart's `wait: true` bought: the SecretProviderClass CRD
    // exists and the CSI socket is registered before any app pod mounts.
    const ascpAddon = new eks.CfnAddon(this, "SecretsStoreCsiDriverAwsAddon", {
      addonName: "aws-secrets-store-csi-driver-provider",
      // Same version the chart was pinned to. Left explicit rather than
      // defaulted so an upgrade is a visible change here.
      addonVersion: "v3.1.2-eksbuild.1",
      clusterName: cluster.clusterName,
      resolveConflicts: "OVERWRITE",
      configurationValues: JSON.stringify({
        "secrets-store-csi-driver": {
          // Upstream default is FALSE. Without this the driver mounts secrets
          // as files but never creates the `secretObjects` Kubernetes Secret,
          // so `secretKeyRef` still fails.
          syncSecret: { enabled: true },
        },
      }),
    });
    ascpAddon.node.addDependency(systemNg);

    // ═══════════════════════════════════════════════════════════════════
    // 9. Workload Namespace + RBAC + Pod Security
    // ═══════════════════════════════════════════════════════════════════
    const appNs = cluster.addManifest("AppNamespace", {
      apiVersion: "v1",
      kind: "Namespace",
      metadata: {
        name: "xrpl-agentic-payments",
        labels: {
          "pod-security.kubernetes.io/enforce": "restricted",
          "pod-security.kubernetes.io/warn": "restricted",
          "elbv2.k8s.aws/pod-readiness-gate-inject": "enabled",
        },
      },
    });

    appNs.node.addDependency(lbChart);

    // Service account for pods
    const appSa = cluster.addManifest("AppServiceAccount", {
      apiVersion: "v1",
      kind: "ServiceAccount",
      metadata: {
        name: "xrpl-agentic-payments-sa",
        namespace: "xrpl-agentic-payments",
      },
    });
    appSa.node.addDependency(appNs);

    // EKS Pod Identity agent — the mechanism podRole is already written for
    // (it trusts pods.eks.amazonaws.com and allows sts:TagSession). Without
    // the agent and the association below, nothing can assume podRole: the
    // app pods get no AWS credentials and the ASCP mount fails with a
    // credentials error, leaving pods in ContainerCreating.
    const podIdentityAgent = new eks.CfnAddon(this, "PodIdentityAgentAddon", {
      addonName: "eks-pod-identity-agent",
      clusterName: cluster.clusterName,
      resolveConflicts: "OVERWRITE",
    });
    podIdentityAgent.node.addDependency(systemNg);

    const podIdentityAssociation = new eks.CfnPodIdentityAssociation(
      this,
      "AppPodIdentityAssociation",
      {
        clusterName: cluster.clusterName,
        namespace: "xrpl-agentic-payments",
        serviceAccount: "xrpl-agentic-payments-sa",
        roleArn: podRole.roleArn,
      }
    );
    podIdentityAssociation.node.addDependency(appSa);

    // SecretProviderClass: what to fetch from Secrets Manager, and which
    // Kubernetes Secret to sync it into. `secretObjects[].data[].objectName`
    // refers to the mounted FILE name (the jmesPath objectAlias below), not
    // to the Secrets Manager secret name.
    const webAuthSpc = cluster.addManifest("WebAuthSecretProviderClass", {
      apiVersion: "secrets-store.csi.x-k8s.io/v1",
      kind: "SecretProviderClass",
      metadata: {
        name: WEB_AUTH_SPC_NAME,
        namespace: "xrpl-agentic-payments",
      },
      spec: {
        provider: "aws",
        secretObjects: [
          {
            secretName: WEB_AUTH_K8S_SECRET,
            type: "Opaque",
            data: [
              { objectName: "webAuthUsername", key: "username" },
              { objectName: "webAuthPassword", key: "password" },
            ],
          },
        ],
        parameters: {
          region,
          // Authenticate as podRole via the Pod Identity association above
          // (omitting this makes the provider fall back to IRSA, which this
          // cluster does not configure for the app service account).
          usePodIdentity: "true",
          // `objects` is a YAML *string*, per the ASCP schema.
          objects: [
            `- objectName: "${WEB_AUTH_SECRET_NAME}"`,
            `  objectType: "secretsmanager"`,
            `  jmesPath:`,
            `    - path: username`,
            `      objectAlias: webAuthUsername`,
            `    - path: password`,
            `      objectAlias: webAuthPassword`,
          ].join("\n"),
        },
      },
    });
    webAuthSpc.node.addDependency(appNs, ascpAddon);

    // There is deliberately NO SecretProviderClass for the wallet seeds here
    // any more. The web tier read config/wallets.json for exactly two public
    // addresses, which now arrive as env vars (see XrplAddresses), so nothing in
    // this pod needs the secret — and an unmounted SecretProviderClass would
    // materialise nothing while implying otherwise to the next reader. The
    // Lambda tools and the AgentCore Runtime MCP server still read
    // xrpl-agentic-payments/wallets themselves at invoke time; that is where
    // signing happens and where the seed belongs.

    // ═══════════════════════════════════════════════════════════════════
    // 10. Application Deployment + Service + HPA + PDB
    // ═══════════════════════════════════════════════════════════════════
    const appDeployment = cluster.addManifest("AppDeployment", {
      apiVersion: "apps/v1",
      kind: "Deployment",
      metadata: { name: "xrpl-agentic-payments-web", namespace: "xrpl-agentic-payments" },
      spec: {
        replicas: 2,
        strategy: {
          rollingUpdate: { maxSurge: 1, maxUnavailable: 0 },
        },
        selector: { matchLabels: { app: "xrpl-agentic-payments-web" } },
        template: {
          metadata: { labels: { app: "xrpl-agentic-payments-web" } },
          spec: {
            serviceAccountName: "xrpl-agentic-payments-sa",
            terminationGracePeriodSeconds: 45,
            securityContext: {
              runAsNonRoot: true,
              runAsUser: 1000,
              fsGroup: 1000,
              seccompProfile: { type: "RuntimeDefault" },
            },
            dnsConfig: {
              options: [
                { name: "ndots", value: "2" },
                { name: "edns0" },
              ],
            },
            topologySpreadConstraints: [
              {
                maxSkew: 1,
                topologyKey: "topology.kubernetes.io/zone",
                whenUnsatisfiable: "DoNotSchedule",
                labelSelector: { matchLabels: { app: "xrpl-agentic-payments-web" } },
              },
            ],
            containers: [
              {
                name: "web",
                image: `${account}.dkr.ecr.${region}.amazonaws.com/xrpl-agentic-payments-webapp:${imageTag}`,
                ports: [{ containerPort: 8080, protocol: "TCP" }],
                env: [
                  { name: "AWS_DEFAULT_REGION", value: region },
                  {
                    name: "XRPL_AGENTIC_USERNAME",
                    valueFrom: {
                      secretKeyRef: {
                        name: WEB_AUTH_K8S_SECRET,
                        key: "username",
                      },
                    },
                  },
                  {
                    name: "XRPL_AGENTIC_PASSWORD",
                    valueFrom: {
                      secretKeyRef: {
                        name: WEB_AUTH_K8S_SECRET,
                        key: "password",
                      },
                    },
                  },
                  // Plain env, not a secretKeyRef: these are public XRPL
                  // addresses. They are here so the pod does NOT have to mount
                  // the wallets secret — see XrplAddresses. If they are ever
                  // removed, orchestrator.py falls back to config/wallets.json,
                  // which in this pod does not exist, and payments fail with a
                  // message naming both variables rather than silently reading
                  // seeds again.
                  {
                    name: "XRPL_EXECUTION_ADDRESS",
                    value: props.xrplAddresses.executionAddress,
                  },
                  {
                    name: "XRPL_RLUSD_ISSUER",
                    value: props.xrplAddresses.rlusdIssuer,
                  },
                  // The tool path. There is no boto3 invoke_gateway API, so the
                  // orchestrator signs a POST to this URL itself — it needs the
                  // endpoint, not an ARN. Unset, src/agentcore_client.py raises
                  // at the first tool call naming this variable, rather than
                  // falling back to a path that bypasses the Gateway.
                  {
                    name: "AGENTCORE_GATEWAY_URL",
                    value: props.gateway.url,
                  },
                  {
                    name: "AGENTCORE_GATEWAY_ID",
                    value: props.gateway.id,
                  },
                  // Pinned by the same constant that pins the gateway's
                  // supportedVersions, so the client cannot ask for a protocol
                  // version the gateway declines.
                  {
                    name: "AGENTCORE_MCP_PROTOCOL_VERSION",
                    value: MCP_PROTOCOL_VERSION,
                  },
                ],
                // The CSI volume MUST be mounted by a pod for the driver to
                // create/refresh WEB_AUTH_K8S_SECRET — a SecretProviderClass
                // on its own syncs nothing. Kubelet sets volumes up before it
                // resolves container env vars, so the secretKeyRef above is
                // satisfied by the time the container starts.
                // No wallets mount. The web tier needed the wallets secret only
                // to read two PUBLIC addresses out of a file full of private
                // seeds; those addresses now arrive as env vars above, so this
                // pod has no seed on its filesystem and none in its memory.
                // Signing was never here — the MCP server on AgentCore Runtime
                // and the Lambda tools read the seed themselves at invoke time.
                volumeMounts: [
                  {
                    name: "web-auth-store",
                    mountPath: "/mnt/secrets-store",
                    readOnly: true,
                  },
                ],
                resources: {
                  requests: { cpu: "500m", memory: "512Mi" },
                  limits: { cpu: "2000m", memory: "1Gi" },
                },
                securityContext: {
                  allowPrivilegeEscalation: false,
                  capabilities: { drop: ["ALL"] },
                },
                readinessProbe: {
                  httpGet: { path: "/health", port: 8080 },
                  initialDelaySeconds: 5,
                  periodSeconds: 10,
                },
                livenessProbe: {
                  httpGet: { path: "/health", port: 8080 },
                  initialDelaySeconds: 15,
                  periodSeconds: 20,
                  failureThreshold: 3,
                },
                lifecycle: {
                  preStop: {
                    exec: { command: ["/bin/sh", "-c", "sleep 10"] },
                  },
                },
              },
            ],
            volumes: [
              {
                name: "web-auth-store",
                csi: {
                  driver: "secrets-store.csi.k8s.io",
                  readOnly: true,
                  volumeAttributes: {
                    secretProviderClass: WEB_AUTH_SPC_NAME,
                  },
                },
              },
            ],
          },
        },
      },
    });
    // The pods cannot start until the CSI driver, the SecretProviderClass and
    // the Pod Identity plumbing that materialise WEB_AUTH_K8S_SECRET all exist.
    appDeployment.node.addDependency(
      appSa,
      webAuthSpc,
      podIdentityAgent,
      podIdentityAssociation
    );

    // Service
    const appService = cluster.addManifest("AppService", {
      apiVersion: "v1",
      kind: "Service",
      metadata: {
        name: "xrpl-agentic-payments-web",
        namespace: "xrpl-agentic-payments",
        annotations: {
          "service.kubernetes.io/topology-mode": "Auto",
        },
      },
      spec: {
        selector: { app: "xrpl-agentic-payments-web" },
        ports: [{ port: 8080, targetPort: 8080 }],
        type: "ClusterIP",
      },
    });
    appService.node.addDependency(appDeployment);

    // Ingress (ALB with WebSocket optimizations). HTTPS on the supplied ACM
    // cert; port 80 exists only to be redirected (ssl-redirect below), so no
    // request is ever served in cleartext.
    const baseIngressAnnotations: Record<string, string> = {
      "alb.ingress.kubernetes.io/scheme": "internet-facing",
      "alb.ingress.kubernetes.io/target-type": "ip",
      "alb.ingress.kubernetes.io/target-group-attributes":
        "stickiness.enabled=true,stickiness.type=app_cookie,stickiness.app_cookie.cookie_name=xrpl_agentic_payments_session,stickiness.app_cookie.duration_seconds=86400,deregistration_delay.timeout_seconds=30",
      "alb.ingress.kubernetes.io/load-balancer-attributes":
        "idle_timeout.timeout_seconds=3600,routing.http2.enabled=true,routing.http.drop_invalid_header_fields.enabled=true",
      "alb.ingress.kubernetes.io/healthcheck-path": "/health",
      "alb.ingress.kubernetes.io/healthcheck-interval-seconds": "15",
      "alb.ingress.kubernetes.io/success-codes": "200",
      "alb.ingress.kubernetes.io/tags": "Project=XrplAgenticPayments",
    };

    const ingressAnnotations: Record<string, string> = {
      ...baseIngressAnnotations,
      "alb.ingress.kubernetes.io/certificate-arn": domain.certificateArn,
      "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443},{"HTTP":80}]',
      "alb.ingress.kubernetes.io/ssl-redirect": "443",
      "alb.ingress.kubernetes.io/ssl-policy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
    };

    const ingressRule: Record<string, unknown> = {
      host: domain.hostname,
      http: {
        paths: [
          {
            path: "/",
            pathType: "Prefix",
            backend: {
              service: { name: "xrpl-agentic-payments-web", port: { number: 8080 } },
            },
          },
        ],
      },
    };

    const appIngress = cluster.addManifest("AppIngress", {
      apiVersion: "networking.k8s.io/v1",
      kind: "Ingress",
      metadata: {
        name: "xrpl-agentic-payments-ingress",
        namespace: "xrpl-agentic-payments",
        annotations: ingressAnnotations,
      },
      spec: {
        ingressClassName: "alb",
        rules: [ingressRule],
      },
    });
    appIngress.node.addDependency(appService);

    // HPA (depends on deployment which depends on namespace)
    const appHpa = cluster.addManifest("AppHPA", {
      apiVersion: "autoscaling/v2",
      kind: "HorizontalPodAutoscaler",
      metadata: { name: "xrpl-agentic-payments-web-hpa", namespace: "xrpl-agentic-payments" },
      spec: {
        scaleTargetRef: {
          apiVersion: "apps/v1",
          kind: "Deployment",
          name: "xrpl-agentic-payments-web",
        },
        minReplicas: 2,
        maxReplicas: 20,
        behavior: {
          scaleUp: {
            stabilizationWindowSeconds: 30,
            policies: [{ type: "Percent", value: 100, periodSeconds: 30 }],
          },
          scaleDown: {
            stabilizationWindowSeconds: 300,
            policies: [{ type: "Percent", value: 25, periodSeconds: 60 }],
          },
        },
        metrics: [
          {
            type: "Resource",
            resource: {
              name: "cpu",
              target: { type: "Utilization", averageUtilization: 60 },
            },
          },
          {
            type: "Resource",
            resource: {
              name: "memory",
              target: { type: "Utilization", averageUtilization: 75 },
            },
          },
        ],
      },
    });

    appHpa.node.addDependency(appDeployment);

    // PDB
    const appPdb = cluster.addManifest("AppPDB", {
      apiVersion: "policy/v1",
      kind: "PodDisruptionBudget",
      metadata: { name: "xrpl-agentic-payments-web-pdb", namespace: "xrpl-agentic-payments" },
      spec: {
        minAvailable: 1,
        selector: { matchLabels: { app: "xrpl-agentic-payments-web" } },
      },
    });

    appPdb.node.addDependency(appDeployment);

    // Network Policy
    const appNetpol = cluster.addManifest("AppNetworkPolicy", {
      apiVersion: "networking.k8s.io/v1",
      kind: "NetworkPolicy",
      metadata: { name: "xrpl-agentic-payments-web-netpol", namespace: "xrpl-agentic-payments" },
      spec: {
        podSelector: { matchLabels: { app: "xrpl-agentic-payments-web" } },
        policyTypes: ["Ingress", "Egress"],
        ingress: [{ from: [], ports: [{ port: 8080 }] }],
        egress: [
          { to: [], ports: [{ port: 443 }, { port: 51234 }] },
          {
            to: [
              {
                namespaceSelector: {
                  matchLabels: {
                    "kubernetes.io/metadata.name": "kube-system",
                  },
                },
              },
            ],
            ports: [{ port: 53, protocol: "UDP" }],
          },
        ],
      },
    });

    appNetpol.node.addDependency(appDeployment);

    // ═══════════════════════════════════════════════════════════════════
    // 11. Security Group for Karpenter Nodes
    // ═══════════════════════════════════════════════════════════════════
    const nodeSg = new ec2.SecurityGroup(this, "KarpenterNodeSG", {
      vpc,
      description: "XRPL Agentic Payments Karpenter worker nodes",
      allowAllOutbound: true,
    });
    nodeSg.addIngressRule(nodeSg, ec2.Port.allTraffic(), "Node-to-node");
    nodeSg.addIngressRule(
      ec2.Peer.ipv4(vpc.vpcCidrBlock),
      ec2.Port.allTraffic(),
      "VPC internal"
    );
    cdk.Tags.of(nodeSg).add(KARPENTER_DISCOVERY_TAG, KARPENTER_DISCOVERY_VALUE);

    // ═══════════════════════════════════════════════════════════════════
    // 12. Outputs
    // ═══════════════════════════════════════════════════════════════════
    new cdk.CfnOutput(this, "ClusterName", {
      value: cluster.clusterName,
    });
    new cdk.CfnOutput(this, "ClusterArn", {
      value: cluster.clusterArn,
    });
    new cdk.CfnOutput(this, "KubeconfigCommand", {
      value: `aws eks update-kubeconfig --name ${CLUSTER_NAME} --region ${region}`,
    });
    new cdk.CfnOutput(this, "PodRoleArn", {
      value: podRole.roleArn,
      description:
        "IAM role assumed by pods in the xrpl-agentic-payments namespace. The " +
        "Pod Identity association for xrpl-agentic-payments-sa is created by " +
        "this stack — no manual step required.",
    });
    new cdk.CfnOutput(this, "KarpenterNodeRoleArn", {
      value: karpenterNodeRole.roleArn,
    });

    // ═══════════════════════════════════════════════════════════════════
    // Tags
    // ═══════════════════════════════════════════════════════════════════
    cdk.Tags.of(this).add("Project", "XrplAgenticPayments");
    cdk.Tags.of(this).add("Environment", "development");
    cdk.Tags.of(this).add("ManagedBy", "cdk");
    cdk.Tags.of(this).add("Phase", "prototype");
  }
}
