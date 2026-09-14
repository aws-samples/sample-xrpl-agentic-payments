/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import { Construct } from "constructs";
import * as fs from "fs";
import * as path from "path";

/**
 * MCP protocol version spoken between the orchestrator and the Gateway.
 *
 * A gateway only accepts versions listed in its
 * `protocolConfiguration.mcp.supportedVersions`, so this single constant is
 * written into the gateway below AND handed to the pod as
 * AGENTCORE_MCP_PROTOCOL_VERSION (infra/lib/eks-stack.ts). Pinned rather than
 * left to the service default so a version the client sends can never be one
 * the gateway declines: with the two derived from one constant, a mismatch is
 * impossible instead of being a 400 on the first tool call after a deploy.
 */
export const MCP_PROTOCOL_VERSION = "2025-11-25";

/**
 * Separator the Gateway puts between a target name and a tool name.
 *
 * Gateway namespaces every tool it exposes as `<targetName>___<toolName>` so
 * tools from different targets cannot collide. Three underscores, matching
 * `strip_tool_prefix` in functions/shared.py, which is what the Lambdas use to
 * recover the bare name. Target names are restricted to alphanumerics and
 * hyphens by the CreateGatewayTarget API — no underscores — so the FIRST `___`
 * in a tool name is always the separator and never part of the target name.
 */
export const GATEWAY_TOOL_SEPARATOR = "___";

/** JSON-Schema description of one tool's arguments. */
interface ToolInputSchema {
  readonly properties: Record<
    string,
    { readonly type: string; readonly description: string }
  >;
  readonly required?: string[];
}

interface ToolTarget {
  /** The Lambda that implements this tool. */
  readonly fn: lambda.Function;
  /**
   * Gateway target name. Alphanumerics and hyphens only, and it becomes the
   * prefix the orchestrator has to send, so it is the kebab-case form of the
   * tool name and nothing more inventive.
   */
  readonly targetName: string;
  /** Bare tool name — the Python function name in functions/<tool>.py. */
  readonly toolName: string;
  readonly description: string;
  readonly inputSchema: ToolInputSchema;
}

/**
 * File name of the Lambda layer archive, shared with the build script.
 *
 * layers/xrpl/build_layer.sh writes exactly this name, and infra/test asserts
 * the two agree. They used to disagree silently — the script produced
 * xrpl-layer.zip while the stack read s3://<artifacts>/layers/xrpl-layer-v1.zip,
 * an object nothing ever uploaded, so `cdk deploy` failed on a key that looked
 * like someone else's job to create.
 */
export const XRPL_LAYER_ZIP_FILENAME = "xrpl-layer-v1.zip";

/** Default location of the built layer archive within a repo checkout. */
export const XRPL_LAYER_ZIP_PATH = path.join(
  __dirname,
  "../../layers/xrpl",
  XRPL_LAYER_ZIP_FILENAME
);

export interface XrplAgenticPaymentsToolsStackProps extends cdk.StackProps {
  /**
   * Overrides the layer archive location. Only tests set this: they point it at
   * a temporary file so a unit test does not require a real ARM64 layer build
   * in the working tree.
   */
  xrplLayerZipPath?: string;
}

/**
 * XrplAgenticPaymentsToolsStack
 *
 * Deploys 8 Lambda functions (one per XRPL tool) + shared Lambda Layer, and the
 * AgentCore Gateway that fronts them.
 *
 * Architecture:
 *   agents (EKS pod) → Gateway → Lambda (tool) → XRPL Testnet
 *
 * The Gateway is declared here rather than by a setup script because it is the
 * only thing that makes those Lambdas reachable: before this, the stack granted
 * bedrock-agentcore permission to invoke them and stopped there, so nothing
 * created a gateway, no target existed, and the orchestrator went straight to
 * the Runtime-hosted MCP server instead — the eight Lambdas were deployed and
 * unreachable, and the architecture diagrams described a hop that never ran.
 *
 * Each function:
 *   - ARM64 (Graviton2) for cost efficiency
 *   - Python 3.13 runtime
 *   - Shared xrpl-py layer (3.4MB)
 *   - Reads wallet config from Secrets Manager at init time
 *   - XRPL client connection persists across invocations
 */
export class XrplAgenticPaymentsToolsStack extends cdk.Stack {
  /**
   * MCP endpoint of the Gateway, e.g.
   * `https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`.
   *
   * Consumed by the EKS stack, which passes it to the pod as
   * AGENTCORE_GATEWAY_URL. There is no boto3 `invoke_gateway` API — a gateway is
   * called as a SigV4-signed POST to this URL — so the URL, not an ARN, is what
   * the client needs.
   */
  public readonly gatewayUrl: string;

  /** Gateway ARN, for scoping bedrock-agentcore:InvokeGateway on the caller. */
  public readonly gatewayArn: string;

  /** Gateway identifier, surfaced in the web UI's infrastructure panel. */
  public readonly gatewayId: string;

  constructor(scope: Construct, id: string, props?: XrplAgenticPaymentsToolsStackProps) {
    super(scope, id, props);

    // ─────────────────────────────────────────────────────────────────
    // 1. Reference existing resources
    // ─────────────────────────────────────────────────────────────────

    const artifactsBucket = s3.Bucket.fromBucketName(
      this,
      "ArtifactsBucket",
      `xrpl-agentic-payments-artifacts-${cdk.Stack.of(this).account}`
    );

    // OFAC SDN dataset location inside the artifacts bucket. These two values
    // MUST stay in sync with S3_KEY in functions/screen_sanctions.py, which
    // reads s3://$ARTIFACTS_BUCKET/data/ofac_sdn.csv at module import time.
    const OFAC_SDN_PREFIX = "data";
    const OFAC_SDN_KEY = `${OFAC_SDN_PREFIX}/ofac_sdn.csv`;

    const walletsSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "WalletsSecret",
      "xrpl-agentic-payments/wallets"
    );

    // ─────────────────────────────────────────────────────────────────
    // 2. Lambda Layer (xrpl-py + httpx + nest_asyncio)
    // ─────────────────────────────────────────────────────────────────

    // The archive is a CDK asset, not a hand-uploaded S3 object: CDK hashes the
    // file and uploads it as part of `cdk deploy`, so the layer content and the
    // stack that consumes it can never drift, and there is no key for a human
    // to typo or forget to upload. Nothing else in the repo uploads to
    // s3://<artifacts>/layers/, which is why the previous fromBucket() key
    // referenced an object that never existed.
    const layerZipPath = props?.xrplLayerZipPath ?? XRPL_LAYER_ZIP_PATH;
    if (!fs.existsSync(layerZipPath)) {
      throw new Error(
        `Lambda layer archive not found: ${layerZipPath}\n` +
          "Build it first (it cross-compiles xrpl-py for ARM64/Graviton):\n" +
          "  ./layers/xrpl/build_layer.sh"
      );
    }

    const xrplLayer = new lambda.LayerVersion(this, "XrplLayer", {
      layerVersionName: "xrpl-agentic-payments-xrpl-layer",
      description: "xrpl-py 5.0.0, httpx, nest_asyncio, pycryptodome (ARM64)",
      code: lambda.Code.fromAsset(layerZipPath),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_14],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
    });

    // ─────────────────────────────────────────────────────────────────
    // 2b. OFAC SDN dataset upload
    // ─────────────────────────────────────────────────────────────────

    // screen_sanctions.py fetches this object on every cold start. Nothing
    // else in the repo uploads it (deploy/package.sh only bundles data/ into
    // the AgentCore Runtime zip, which is a different artifact), so without
    // this deployment the Lambda import fails with NoSuchKey even once the
    // bucket name and IAM grant are correct.
    //
    // prune: false because the artifacts bucket is NOT owned by this stack (it
    // is referenced by name and shared with other deployment artifacts) —
    // pruning would delete anything else that lands under data/.
    const ofacDataset = new s3deploy.BucketDeployment(this, "OfacSdnDataset", {
      // Uploads the whole repo-root data/ directory (currently just
      // ofac_sdn.csv) to s3://<artifacts-bucket>/data/ — no exclude filter, so
      // there is no glob pattern that can silently produce an empty asset.
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../data"))],
      destinationBucket: artifactsBucket,
      destinationKeyPrefix: OFAC_SDN_PREFIX,
      prune: false,
      retainOnDelete: false,
      memoryLimit: 512, // 5.4MB CSV — default 128MB is tight for unzip+copy
    });

    // ─────────────────────────────────────────────────────────────────
    // 3. IAM Roles (least privilege)
    // ─────────────────────────────────────────────────────────────────

    // Read-only role: for tools that only query XRPL (no wallet signing needed,
    // but we still load secrets for RPC URL and metadata)
    const readOnlyRole = new iam.Role(this, "ToolReadOnlyRole", {
      roleName: "xrpl-agentic-payments-tool-readonly",
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "XRPL Agentic Payments read-only tool Lambda role (no wallet seed access needed for signing, but reads metadata)",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole"
        ),
      ],
    });

    // Grant read access to wallets secret (all tools need RPC URL from metadata)
    walletsSecret.grantRead(readOnlyRole);

    // screen_sanctions (the only tool that touches S3) loads the OFAC SDN CSV
    // from the artifacts bucket at cold start. Scoped to that single object
    // key rather than the whole bucket, because readOnlyRole is shared by all
    // seven read-only tools and the bucket also holds deployment artifacts.
    artifactsBucket.grantRead(readOnlyRole, OFAC_SDN_KEY);

    // Write role: for submit_payment which signs and submits transactions
    const writeRole = new iam.Role(this, "ToolWriteRole", {
      roleName: "xrpl-agentic-payments-tool-write",
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description: "XRPL Agentic Payments write tool Lambda role (signs XRPL transactions)",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole"
        ),
      ],
    });

    walletsSecret.grantRead(writeRole);

    // Sanctions role: for screen_sanctions (no XRPL access needed at all,
    // but shared.py loads secrets unconditionally for simplicity)
    // Using readOnlyRole for this since the overhead is minimal

    // ─────────────────────────────────────────────────────────────────
    // 4. Shared Lambda configuration
    // ─────────────────────────────────────────────────────────────────

    const functionsDir = path.join(__dirname, "../../functions");

    const RUNTIME = lambda.Runtime.PYTHON_3_14;
    const ARCH = lambda.Architecture.ARM_64;
    const COMMON_ENV = {
      POWERTOOLS_SERVICE_NAME: "xrpl-agentic-payments",
      LOG_LEVEL: "INFO",
    };

    // ─────────────────────────────────────────────────────────────────
    // 5. Lambda Functions (8 tools)
    // ─────────────────────────────────────────────────────────────────

    // --- Read-only tools ---

    const getBalance = new lambda.Function(this, "GetBalance", {
      functionName: "xrpl-agentic-payments-get-balance",
      description: "Get XRP and token balances for an XRPL account",
      handler: "get_balance.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const checkTransaction = new lambda.Function(this, "CheckTransaction", {
      functionName: "xrpl-agentic-payments-check-transaction",
      description: "Check status and details of an XRPL transaction by hash",
      handler: "check_transaction.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const getTrustLines = new lambda.Function(this, "GetTrustLines", {
      functionName: "xrpl-agentic-payments-get-trust-lines",
      description: "Get all trust lines (token relationships) for an XRPL account",
      handler: "get_trust_lines.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const getOrderbook = new lambda.Function(this, "GetOrderbook", {
      functionName: "xrpl-agentic-payments-get-orderbook",
      description: "Query the XRPL DEX order book for a trading pair",
      handler: "get_orderbook.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const getPaths = new lambda.Function(this, "GetPaths", {
      functionName: "xrpl-agentic-payments-get-paths",
      description: "Find optimal payment paths with cost estimates on XRPL",
      handler: "get_paths.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const pathFind = new lambda.Function(this, "PathFind", {
      functionName: "xrpl-agentic-payments-path-find",
      description: "Find cross-currency payment paths on XRPL",
      handler: "path_find.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    const screenSanctions = new lambda.Function(this, "ScreenSanctions", {
      functionName: "xrpl-agentic-payments-screen-sanctions",
      description: "Screen entity against official OFAC SDN list (U.S. Treasury)",
      handler: "screen_sanctions.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: readOnlyRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      environment: {
        ...COMMON_ENV,
        // Required. screen_sanctions.py reads ARTIFACTS_BUCKET at module
        // import time; with it unset the fallback resolves to the invalid
        // name "xrpl-agentic-payments-artifacts-" (AWS_ACCOUNT_ID is never
        // set in Lambda) and every cold start dies with NoSuchBucket.
        ARTIFACTS_BUCKET: artifactsBucket.bucketName,
      },
    });

    // The dataset must exist in S3 before the function can be invoked.
    screenSanctions.node.addDependency(ofacDataset);

    // --- Write tool (signs and submits XRPL transactions) ---

    const submitPayment = new lambda.Function(this, "SubmitPayment", {
      functionName: "xrpl-agentic-payments-submit-payment",
      description: "Sign and submit XRPL Payment transaction (WRITES TO LEDGER)",
      handler: "submit_payment.lambda_handler",
      code: lambda.Code.fromAsset(functionsDir),
      role: writeRole,
      runtime: RUNTIME,
      architecture: ARCH,
      layers: [xrplLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: COMMON_ENV,
    });

    // ─────────────────────────────────────────────────────────────────
    // 5b. Log groups go away with the stack
    // ─────────────────────────────────────────────────────────────────

    // cdk.json turns on @aws-cdk/aws-lambda:useCdkManagedLogGroup, so each
    // Function above also emits an AWS::Logs::LogGroup — and CDK's LogGroup
    // defaults to DeletionPolicy: Retain. Every function here has a fixed
    // functionName, so the log group names are fixed too, and a create that
    // rolls back leaves all of them behind. The next attempt then fails on
    //
    //   /aws/lambda/xrpl-agentic-payments-get-balance already exists
    //
    // for a log group holding zero bytes, and stays failed until someone
    // sweeps them by hand. That happened twice on the first deploy of this
    // stack: 7 orphans, then 8.
    //
    // Retain is a sound default for a log group meant to outlive its
    // function. It is the wrong one here, where the group's whole reason to
    // exist is the function CloudFormation just gave up on creating.
    //
    // Set with RemovalPolicies rather than per-function props so it also
    // reaches the log group of the BucketDeployment handler CDK synthesizes
    // for OfacSdnDataset, which this stack never names and cannot pass props
    // to. RetentionInDays is untouched — two-year retention is what ages logs
    // out; this only decides who owns them once the stack is gone.
    cdk.RemovalPolicies.of(this).destroy({
      applyToResourceTypes: [logs.CfnLogGroup.CFN_RESOURCE_TYPE_NAME],
    });

    // ─────────────────────────────────────────────────────────────────
    // 6. AgentCore Gateway — the MCP front door for the 8 tools
    // ─────────────────────────────────────────────────────────────────

    // An XRPL account, used for every address-shaped argument below.
    const ACCOUNT = "XRPL account address (r-prefixed, e.g. rEXAMPLE...)";

    // One target per Lambda, because a Lambda target maps ONE function to its
    // tool schema and each tool here is its own function.
    //
    // Every `properties` key below has to match what the handler reads out of
    // `event` (functions/<tool>.py), and every `required` entry has to match the
    // handler's own missing-parameter check — the Gateway validates arguments
    // against this schema, so a name that disagrees means the tool is either
    // rejected at the Gateway or reached with the parameter silently absent.
    const toolTargets: ToolTarget[] = [
      {
        fn: getBalance,
        targetName: "get-balance",
        toolName: "get_balance",
        description: "Get XRP and token balances for an XRPL account.",
        inputSchema: {
          properties: { account: { type: "string", description: ACCOUNT } },
          required: ["account"],
        },
      },
      {
        fn: checkTransaction,
        targetName: "check-transaction",
        toolName: "check_transaction",
        description:
          "Check the validation status and details of an XRPL transaction.",
        inputSchema: {
          properties: {
            tx_hash: { type: "string", description: "XRPL transaction hash." },
          },
          required: ["tx_hash"],
        },
      },
      {
        fn: getTrustLines,
        targetName: "get-trust-lines",
        toolName: "get_trust_lines",
        description: "Get all trust lines (token relationships) for an account.",
        inputSchema: {
          properties: { account: { type: "string", description: ACCOUNT } },
          required: ["account"],
        },
      },
      {
        fn: getOrderbook,
        targetName: "get-orderbook",
        toolName: "get_orderbook",
        description: "Query the XRPL DEX order book for a trading pair.",
        inputSchema: {
          // No `required`: get_orderbook.py defaults every parameter, so an
          // empty argument object is a legitimate call (USD/XRP, limit 10).
          properties: {
            base_currency: {
              type: "string",
              description: 'Base currency code, e.g. "USD". Defaults to USD.',
            },
            base_issuer: {
              type: "string",
              description: "Issuer of the base currency. Omit for XRP.",
            },
            quote_currency: {
              type: "string",
              description: 'Quote currency code, e.g. "XRP". Defaults to XRP.',
            },
            quote_issuer: {
              type: "string",
              description: "Issuer of the quote currency. Omit for XRP.",
            },
            limit: {
              type: "integer",
              description: "Number of order book levels to return (default 10).",
            },
          },
        },
      },
      {
        fn: getPaths,
        targetName: "get-paths",
        toolName: "get_paths",
        description:
          "Find payment paths with cost estimates for a destination amount.",
        inputSchema: {
          properties: {
            source: { type: "string", description: `Source ${ACCOUNT}` },
            destination: {
              type: "string",
              description: `Destination ${ACCOUNT}`,
            },
            dest_amount: {
              type: "string",
              description: "Destination amount, as a decimal string.",
            },
            dest_currency: {
              type: "string",
              description: "Destination currency code. Defaults to USD.",
            },
            dest_issuer: {
              type: "string",
              description: "Destination currency issuer. Omit for XRP.",
            },
          },
          required: ["source", "destination", "dest_amount"],
        },
      },
      {
        fn: pathFind,
        targetName: "path-find",
        toolName: "path_find",
        description: "Find cross-currency payment paths on XRPL.",
        inputSchema: {
          properties: {
            source: { type: "string", description: `Source ${ACCOUNT}` },
            destination: {
              type: "string",
              description: `Destination ${ACCOUNT}`,
            },
            amount: {
              type: "string",
              description: "Amount to deliver, as a decimal string.",
            },
            currency: {
              type: "string",
              description: "Currency code. Defaults to USD.",
            },
            issuer: {
              type: "string",
              description: "Currency issuer. Omit for XRP.",
            },
          },
          required: ["source", "destination", "amount"],
        },
      },
      {
        fn: screenSanctions,
        targetName: "screen-sanctions",
        toolName: "screen_sanctions",
        description:
          "Screen an entity against the official OFAC SDN list (U.S. Treasury).",
        inputSchema: {
          properties: {
            entity_name: {
              type: "string",
              description: "Legal name of the entity or individual to screen.",
            },
            entity_country: {
              type: "string",
              description: "ISO country code, used to weight a match.",
            },
            entity_address: {
              type: "string",
              description: "Street address, used to weight a match.",
            },
          },
          required: ["entity_name"],
        },
      },
      {
        fn: submitPayment,
        targetName: "submit-payment",
        toolName: "submit_payment",
        description:
          "Sign and submit an XRPL Payment transaction. WRITES TO THE LEDGER.",
        inputSchema: {
          properties: {
            destination: {
              type: "string",
              description: `Destination ${ACCOUNT}`,
            },
            amount: {
              type: "string",
              description: "Amount to send, as a decimal string.",
            },
            currency: {
              type: "string",
              description: 'Currency code. Defaults to "USD" (RLUSD).',
            },
            issuer: {
              type: "string",
              description: "Token issuer. Defaults to the configured RLUSD issuer.",
            },
            source_wallet: {
              type: "string",
              description:
                'Which configured wallet signs. Defaults to "execution".',
            },
            // Not a parameter the Lambda needed before the Gateway existed: it
            // used to stamp the memo with its own request id, which correlates
            // nothing. Passing the orchestrator's per-payment session id makes
            // every tool call in one payment share one on-ledger session_id.
            session_id: {
              type: "string",
              description:
                "Orchestrator session id, recorded in the on-ledger attribution memo.",
            },
          },
          required: ["destination", "amount"],
        },
      },
    ];

    // Gateway execution role. Gateway uses the role attached to the GATEWAY to
    // call Lambda targets, so the invoke permission is identity-based on this
    // role rather than a resource policy on each function. That is a narrower
    // grant than what this stack did before: it opened lambda:InvokeFunction to
    // the whole bedrock-agentcore service principal, which authorizes any
    // gateway in any account under that service, not just this one.
    const gatewayRole = new iam.Role(this, "GatewayRole", {
      roleName: "xrpl-agentic-payments-gateway",
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: {
            "aws:SourceArn": `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`,
          },
        },
      }),
      description:
        "Role AgentCore Gateway assumes to invoke the 8 XRPL tool Lambdas",
    });

    for (const target of toolTargets) {
      target.fn.grantInvoke(gatewayRole);
    }

    const gateway = new agentcore.CfnGateway(this, "ToolGateway", {
      name: "xrpl-agentic-payments",
      description: "MCP front door for the 8 XRPL tool Lambdas",
      roleArn: gatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          supportedVersions: [MCP_PROTOCOL_VERSION],
          instructions:
            "XRPL payment, market data and compliance tools on the XRP Ledger testnet.",
        },
      },
      // SigV4, not CUSTOM_JWT. The caller is the orchestrator running in the
      // EKS pod, which already has an AWS identity via EKS Pod Identity — so it
      // can sign requests with no token to mint, store or rotate. A JWT
      // authorizer would mean standing up an OAuth client for a caller that
      // never leaves the account.
      authorizerType: "AWS_IAM",
    });

    for (const target of toolTargets) {
      const gatewayTarget = new agentcore.CfnGatewayTarget(
        this,
        `Target${target.toolName}`,
        {
          gatewayIdentifier: gateway.attrGatewayIdentifier,
          name: target.targetName,
          description: target.description,
          // Required for Lambda targets, even though the CloudFormation
          // reference marks CredentialProviderConfigurations as optional on the
          // resource: the AgentCore API rejects a Lambda target without it
          // ("CredentialProviderConfigurations is required for Lambda targets"),
          // so synth and cdk-nag both pass and it fails only at create time.
          //
          // GATEWAY_IAM_ROLE is the provider that matches gatewayRole above —
          // the Gateway invokes each Lambda as its own execution role, which is
          // what the grantInvoke() loop authorizes. Lambda targets support no
          // other provider (OAUTH/API_KEY are for HTTP/OpenAPI targets), so
          // there is no sub-configuration to supply.
          credentialProviderConfigurations: [
            { credentialProviderType: "GATEWAY_IAM_ROLE" },
          ],
          targetConfiguration: {
            mcp: {
              lambda: {
                lambdaArn: target.fn.functionArn,
                toolSchema: {
                  inlinePayload: [
                    {
                      name: target.toolName,
                      description: target.description,
                      inputSchema: {
                        type: "object",
                        properties: target.inputSchema.properties,
                        ...(target.inputSchema.required
                          ? { required: target.inputSchema.required }
                          : {}),
                      },
                    },
                  ],
                },
              },
            },
          },
        }
      );

      // The role must be able to invoke the function before a target that
      // routes to it exists, or the target's first call fails on AccessDenied.
      gatewayTarget.node.addDependency(gatewayRole);
    }

    this.gatewayUrl = gateway.attrGatewayUrl;
    this.gatewayArn = gateway.attrGatewayArn;
    this.gatewayId = gateway.attrGatewayIdentifier;

    // ─────────────────────────────────────────────────────────────────
    // 7. Outputs
    // ─────────────────────────────────────────────────────────────────

    new cdk.CfnOutput(this, "LayerArn", {
      value: xrplLayer.layerVersionArn,
      description: "Lambda Layer ARN (xrpl-py)",
    });

    new cdk.CfnOutput(this, "GatewayUrl", {
      value: this.gatewayUrl,
      description:
        "AgentCore Gateway MCP endpoint — set as AGENTCORE_GATEWAY_URL to run the orchestrator outside EKS",
    });

    new cdk.CfnOutput(this, "GatewayId", {
      value: this.gatewayId,
      description: "AgentCore Gateway identifier",
    });

    new cdk.CfnOutput(this, "GetBalanceArn", {
      value: getBalance.functionArn,
      description: "get_balance Lambda ARN",
    });

    new cdk.CfnOutput(this, "SubmitPaymentArn", {
      value: submitPayment.functionArn,
      description: "submit_payment Lambda ARN",
    });

    new cdk.CfnOutput(this, "ScreenSanctionsArn", {
      value: screenSanctions.functionArn,
      description: "screen_sanctions Lambda ARN",
    });

    // ─────────────────────────────────────────────────────────────────
    // 8. Cost Allocation Tags
    // ─────────────────────────────────────────────────────────────────

    cdk.Tags.of(this).add("Project", "XrplAgenticPayments");
    cdk.Tags.of(this).add("Environment", "development");
    cdk.Tags.of(this).add("ManagedBy", "cdk");
    cdk.Tags.of(this).add("Phase", "prototype");
  }
}
