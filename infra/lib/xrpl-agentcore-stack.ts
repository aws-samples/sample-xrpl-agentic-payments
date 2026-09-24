import * as path from "node:path";
import {
  Annotations,
  CfnCondition,
  CfnOutput,
  CfnParameter,
  CfnResource,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_apigatewayv2 as apigwv2,
  aws_apigatewayv2_authorizers as authorizers,
  aws_apigatewayv2_integrations as integrations,
  aws_bedrockagentcore as agentcore,
  aws_codebuild as codebuild,
  aws_cognito as cognito,
  aws_dynamodb as dynamodb,
  aws_ecr as ecr,
  aws_iam as iam,
  aws_kms as kms,
  aws_lambda as lambda,
  aws_lambda_event_sources as eventSources,
  aws_logs as logs,
  aws_s3 as s3,
  aws_secretsmanager as secretsmanager,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as tasks,
} from "aws-cdk-lib";
import { Construct } from "constructs";

const DEFAULT_ADDRESS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh";
const DEFAULT_DESTINATION = "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg";
const MCP_VERSION = "2025-06-18";
const DEMO_WEB_ORIGINS = [
  "http://localhost:3000",
  "http://127.0.0.1:3000",
];

export interface XrplAgentCoreStackProps extends StackProps {
  readonly runtimeImageTag?: string;
}

interface GatewayTool {
  readonly id: string;
  readonly targetName: string;
  readonly toolName: string;
  readonly description: string;
  readonly handler: string;
  readonly inputSchema: agentcore.SchemaDefinition;
  readonly access: "none" | "read" | "write";
}

export class XrplAgentCoreStack extends Stack {
  constructor(
    scope: Construct,
    id: string,
    props: XrplAgentCoreStackProps = {},
  ) {
    super(scope, id, props);

    if (!["us-east-1", "us-east-2", "us-west-2"].includes(this.region)) {
      Annotations.of(this).addWarning(
        "This stack invokes Claude Sonnet 4.5 through the 'us.' cross-region " +
          "inference profile, which fans out only to us-east-1, us-east-2, " +
          "and us-west-2. Deploying elsewhere needs Bedrock model access and " +
          "an inference profile for this region, and AgentCore Runtime, " +
          "Gateway, Memory, and Policy available here too.",
      );
    }

    const deployAgentRuntime = new CfnParameter(this, "DeployAgentRuntime", {
      type: "String",
      default: "false",
      allowedValues: ["true", "false"],
      description:
        "Two-phase deployment gate. Set true only after the ARM64 runtime image is in ECR.",
    });
    const deployAgentRuntimeCondition = new CfnCondition(
      this,
      "DeployAgentRuntimeCondition",
      {
        expression: Fn.conditionEquals(
          deployAgentRuntime.valueAsString,
          "true",
        ),
      },
    );

    const repositoryRoot = path.resolve(__dirname, "../..");
    const pythonCode = lambda.Code.fromAsset(
      path.join(repositoryRoot, "src"),
    );
    const dependencyLayer = new lambda.LayerVersion(this, "PythonDependencies", {
      code: lambda.Code.fromAsset(path.join(repositoryRoot, "dist/layer")),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      description: "Pinned runtime dependencies built by scripts/build-lambda-layer.sh",
    });

    const executionAddress = new CfnParameter(this, "XrplExecutionAddress", {
      type: "String",
      default: DEFAULT_ADDRESS,
      description: "Funded XRPL Testnet source account matching the execution seed.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });
    const payoutAddress = new CfnParameter(this, "XrplPayoutAddress", {
      type: "String",
      default: DEFAULT_DESTINATION,
      description: "XRPL Testnet wallet receiving simulated local-fiat settlements.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });
    const feePayerAddress = new CfnParameter(this, "XrplFeePayerAddress", {
      type: "String",
      default: DEFAULT_ADDRESS,
      description: "XRPL Testnet x402 fee payer matching the fee seed.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });
    const feeMerchantAddress = new CfnParameter(this, "XrplFeeMerchantAddress", {
      type: "String",
      default: DEFAULT_DESTINATION,
      description: "XRPL Testnet x402 fee merchant matching the refund seed.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });
    const usdIssuerAddress = new CfnParameter(this, "XrplUsdIssuerAddress", {
      type: "String",
      default: DEFAULT_ADDRESS,
      description: "XRPL Testnet USD fixture issuer.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });
    const mxnIssuerAddress = new CfnParameter(this, "XrplMxnIssuerAddress", {
      type: "String",
      default: DEFAULT_DESTINATION,
      description: "XRPL Testnet MXN fixture issuer.",
      allowedPattern: "^r[1-9A-HJ-NP-Za-km-z]{24,34}$",
    });

    const transferKey = new kms.Key(this, "TransferKey", {
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.DESTROY,
      description: "Encryption for public transfer workflow records",
    });
    const artifactKey = new kms.Key(this, "ArtifactKey", {
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.DESTROY,
      description: "Encryption boundary for signed XRPL transaction artifacts",
    });
    const memoryKey = new kms.Key(this, "MemoryKey", {
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.DESTROY,
      description: "Encryption for opt-in AgentCore transfer preferences",
    });

    const transferTable = new dynamodb.Table(this, "TransferTable", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: transferKey,
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      timeToLiveAttribute: "expires_at",
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    transferTable.addGlobalSecondaryIndex({
      indexName: "owner-created-index",
      partitionKey: { name: "gsi1pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "gsi1sk", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const artifactTable = new dynamodb.Table(this, "ExecutionArtifactTable", {
      partitionKey: {
        name: "transfer_id",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: "kind", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: artifactKey,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const userPool = new cognito.UserPool(this, "UserPool", {
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireDigits: true,
        requireLowercase: true,
        requireSymbols: true,
        requireUppercase: true,
      },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const userPoolClient = userPool.addClient("WebClient", {
      authFlows: {
        userSrp: true,
      },
      preventUserExistenceErrors: true,
      generateSecret: false,
    });

    const preferenceMemory = new agentcore.Memory(this, "PreferenceMemory", {
      memoryName: "XrplTransferPreferences",
      description: "Opt-in structured transfer display and routing preferences",
      expirationDuration: Duration.days(30),
      kmsKey: memoryKey,
      memoryStrategies: [
        agentcore.MemoryStrategy.usingUserPreference({
          strategyName: "TransferPreferences",
          description: "Extract only explicit transfer UI preferences",
          namespaces: ["/preferences/{actorId}/"],
        }),
      ],
    });

    const commonEnvironment = {
      TRANSFER_TABLE_NAME: transferTable.tableName,
      XRPL_NETWORK: "testnet",
      XRPL_RPC_URL: "https://s.altnet.rippletest.net:51234",
      XRPL_EXPLORER_URL: "https://testnet.xrpl.org/transactions",
      XRPL_EXECUTION_ADDRESS: executionAddress.valueAsString,
      XRPL_PAYOUT_ADDRESS: payoutAddress.valueAsString,
      XRPL_FEE_PAYER_ADDRESS: feePayerAddress.valueAsString,
      XRPL_FEE_MERCHANT_ADDRESS: feeMerchantAddress.valueAsString,
      XRPL_USD_ISSUER_ADDRESS: usdIssuerAddress.valueAsString,
      XRPL_MXN_ISSUER_ADDRESS: mxnIssuerAddress.valueAsString,
      XRPL_FIXTURE_RATE: "17.25",
      AGENTCORE_MEMORY_ID: preferenceMemory.memoryId,
    };

    const createFunction = (
      constructId: string,
      handler: string,
      environment: Record<string, string>,
      timeout = Duration.seconds(30),
    ): lambda.Function => {
      const fn = new lambda.Function(this, constructId, {
        runtime: lambda.Runtime.PYTHON_3_13,
        architecture: lambda.Architecture.ARM_64,
        code: pythonCode,
        handler,
        layers: [dependencyLayer],
        timeout,
        memorySize: 512,
        environment,
        tracing: lambda.Tracing.ACTIVE,
        logGroup: new logs.LogGroup(this, `${constructId}Logs`, {
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      });
      return fn;
    };

    const apiFunction = createFunction(
      "ApiFunction",
      "xrpl_agentcore.api.lambda_handler",
      {
        ...commonEnvironment,
        CORS_ORIGINS: DEMO_WEB_ORIGINS.join(","),
      },
    );
    transferTable.grantReadWriteData(apiFunction);
    preferenceMemory.grantWrite(apiFunction);
    memoryKey.grant(
      apiFunction,
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    );

    const httpApi = new apigwv2.HttpApi(this, "TransferApi", {
      apiName: "xrpl-agentcore-transfer-api",
      corsPreflight: {
        allowOrigins: DEMO_WEB_ORIGINS,
        allowHeaders: [
          "authorization",
          "content-type",
          "idempotency-key",
          "x-memory-session",
        ],
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
      },
    });
    const apiIntegration = new integrations.HttpLambdaIntegration(
      "TransferApiIntegration",
      apiFunction,
    );
    const jwtAuthorizer = new authorizers.HttpJwtAuthorizer(
      "CognitoAuthorizer",
      `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`,
      { jwtAudience: [userPoolClient.userPoolClientId] },
    );
    httpApi.addRoutes({
      path: "/health",
      methods: [apigwv2.HttpMethod.GET],
      integration: apiIntegration,
    });
    for (const route of [
      { path: "/v1/corridors", methods: [apigwv2.HttpMethod.GET] },
      { path: "/v1/quotes", methods: [apigwv2.HttpMethod.POST] },
      {
        path: "/v1/transfers",
        methods: [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      },
      {
        path: "/v1/transfers/{transfer_id}",
        methods: [apigwv2.HttpMethod.GET],
      },
      {
        path: "/v1/transfers/{transfer_id}/approve",
        methods: [apigwv2.HttpMethod.POST],
      },
      { path: "/v1/preferences", methods: [apigwv2.HttpMethod.POST] },
    ]) {
      httpApi.addRoutes({
        ...route,
        integration: apiIntegration,
        authorizer: jwtAuthorizer,
      });
    }

    const signerFunction = createFunction(
      "SignerFunction",
      "xrpl_agentcore.execution.lambda_handler",
      {
        ...commonEnvironment,
        EXECUTION_ARTIFACT_TABLE_NAME: artifactTable.tableName,
        EXECUTION_ROLE_MODE: "SIGNER",
        ALLOWED_EXECUTION_TASKS:
          "mark_fee_pending,submit_fee,submit_payment,submit_refund",
        XRPL_EXECUTION_SEED_SECRET_ID:
          "xrpl-agentcore/testnet/execution-seed",
        XRPL_FEE_SEED_SECRET_ID: "xrpl-agentcore/testnet/fee-payer-seed",
        XRPL_FEE_MERCHANT_SEED_SECRET_ID:
          "xrpl-agentcore/testnet/fee-merchant-seed",
      },
      Duration.seconds(45),
    );
    const cfnSigner = signerFunction.node.defaultChild as lambda.CfnFunction;
    cfnSigner.reservedConcurrentExecutions = 1;

    const reconcilerFunction = createFunction(
      "ReconcilerFunction",
      "xrpl_agentcore.execution.lambda_handler",
      {
        ...commonEnvironment,
        EXECUTION_ARTIFACT_TABLE_NAME: artifactTable.tableName,
        EXECUTION_ROLE_MODE: "RECONCILER",
        ALLOWED_EXECUTION_TASKS:
          "reconcile_fee,reconcile_payment,complete_payout,reconcile_refund",
      },
      Duration.seconds(30),
    );
    transferTable.grantReadWriteData(signerFunction);
    transferTable.grantReadWriteData(reconcilerFunction);
    artifactTable.grantReadWriteData(signerFunction);
    artifactTable.grantReadData(reconcilerFunction);

    for (const secretName of [
      "xrpl-agentcore/testnet/execution-seed",
      "xrpl-agentcore/testnet/fee-payer-seed",
      "xrpl-agentcore/testnet/fee-merchant-seed",
    ]) {
      secretsmanager.Secret.fromSecretNameV2(
        this,
        `Imported${secretName.split("/").at(-1)}`,
        secretName,
      ).grantRead(signerFunction);
    }

    const invokeTask = (
      id: string,
      fn: lambda.IFunction,
      task: string,
      includeApprovalHash = false,
    ): tasks.LambdaInvoke => {
      const payload: Record<string, unknown> = {
        task,
        transfer_id: sfn.JsonPath.stringAt("$.transfer_id"),
      };
      if (includeApprovalHash) {
        payload.approval_hash = sfn.JsonPath.stringAt("$.approval_hash");
      }
      const invoke = new tasks.LambdaInvoke(this, id, {
        lambdaFunction: fn,
        payload: sfn.TaskInput.fromObject(payload),
        payloadResponseOnly: true,
        resultPath: "$.result",
      });
      invoke.addRetry({
        errors: ["Lambda.ServiceException", "Lambda.TooManyRequestsException"],
        interval: Duration.seconds(2),
        maxAttempts: 4,
        backoffRate: 2,
      });
      return invoke;
    };

    const markFee = invokeTask(
      "MarkFeePending",
      signerFunction,
      "mark_fee_pending",
      true,
    );
    const submitFee = invokeTask("SubmitFee", signerFunction, "submit_fee");
    const reconcileFee = invokeTask(
      "ReconcileFee",
      reconcilerFunction,
      "reconcile_fee",
    );
    const submitPayment = invokeTask(
      "SubmitPayment",
      signerFunction,
      "submit_payment",
    );
    const reconcilePayment = invokeTask(
      "ReconcilePayment",
      reconcilerFunction,
      "reconcile_payment",
    );
    const completePayout = invokeTask(
      "CompletePayout",
      reconcilerFunction,
      "complete_payout",
    );
    const submitRefund = invokeTask(
      "SubmitFeeRefund",
      signerFunction,
      "submit_refund",
    );
    const reconcileRefund = invokeTask(
      "ReconcileFeeRefund",
      reconcilerFunction,
      "reconcile_refund",
    );
    const feeWait = new sfn.Wait(this, "WaitForFeeLedger", {
      time: sfn.WaitTime.duration(Duration.seconds(4)),
    });
    const paymentWait = new sfn.Wait(this, "WaitForPaymentLedger", {
      time: sfn.WaitTime.duration(Duration.seconds(4)),
    });
    const refundWait = new sfn.Wait(this, "WaitForRefundLedger", {
      time: sfn.WaitTime.duration(Duration.seconds(4)),
    });
    const executionRejected = new sfn.Fail(this, "ExecutionRejected", {
      cause: "Transfer reached a terminal failure before payment settlement",
    });
    const refundHandled = new sfn.Succeed(this, "RefundHandled");
    const feeChoice = new sfn.Choice(this, "FeeReconciled?");
    const paymentChoice = new sfn.Choice(this, "PaymentReconciled?");
    const refundSubmittedChoice = new sfn.Choice(this, "RefundSubmitted?");
    const refundChoice = new sfn.Choice(this, "RefundReconciled?");
    const startChoice = new sfn.Choice(this, "ExecutionCanStart?");
    const submitPaymentChoice = new sfn.Choice(this, "PaymentSubmitted?");
    const resultIsTrue = (path: string): sfn.Condition =>
      sfn.Condition.and(
        sfn.Condition.isPresent(path),
        sfn.Condition.booleanEquals(path, true),
      );

    feeWait.next(reconcileFee).next(feeChoice);
    feeChoice
      .when(resultIsTrue("$.result.terminal"), executionRejected)
      .when(resultIsTrue("$.result.done"), submitPayment)
      .otherwise(feeWait);

    paymentWait.next(reconcilePayment).next(paymentChoice);
    paymentChoice
      .when(resultIsTrue("$.result.refund"), submitRefund)
      .when(resultIsTrue("$.result.done"), completePayout)
      .otherwise(paymentWait);

    submitRefund.next(refundSubmittedChoice);
    refundSubmittedChoice
      .when(resultIsTrue("$.result.done"), refundHandled)
      .otherwise(refundWait);
    refundWait.next(reconcileRefund).next(refundChoice);
    refundChoice
      .when(resultIsTrue("$.result.done"), refundHandled)
      .otherwise(refundWait);

    submitPayment.next(submitPaymentChoice);
    submitPaymentChoice
      .when(resultIsTrue("$.result.refund"), submitRefund)
      .when(resultIsTrue("$.result.terminal"), executionRejected)
      .when(resultIsTrue("$.result.done"), completePayout)
      .otherwise(paymentWait);

    markFee.next(startChoice);
    startChoice
      .when(resultIsTrue("$.result.terminal"), executionRejected)
      .otherwise(submitFee.next(feeWait));

    const stateMachine = new sfn.StateMachine(this, "TransferStateMachine", {
      stateMachineName: "xrpl-agentcore-transfer-poc",
      definitionBody: sfn.DefinitionBody.fromChainable(markFee),
      timeout: Duration.minutes(10),
      tracingEnabled: true,
      logs: {
        destination: new logs.LogGroup(this, "StateMachineLogs", {
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
        includeExecutionData: false,
      },
    });

    const outboxFunction = createFunction(
      "OutboxFunction",
      "xrpl_agentcore.outbox.lambda_handler",
      {
        TRANSFER_STATE_MACHINE_ARN: stateMachine.stateMachineArn,
      },
    );
    stateMachine.grantStartExecution(outboxFunction);
    outboxFunction.addEventSource(
      new eventSources.DynamoEventSource(transferTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        retryAttempts: 5,
        bisectBatchOnError: true,
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual("INSERT"),
            dynamodb: {
              NewImage: {
                event_type: {
                  S: lambda.FilterRule.isEqual("EXECUTE_APPROVED_TRANSFER"),
                },
              },
            },
          }),
        ],
      }),
    );

    const policyEngine = new agentcore.PolicyEngine(this, "PolicyEngine", {
      policyEngineName: "XrplTransferPolicy",
      description: "Default-deny policy for quote and status tools only",
    });
    const gatewayName = "xrpl-transfer-gateway";
    const gateway = new agentcore.Gateway(this, "Gateway", {
      gatewayName,
      description: "Non-custodial AgentCore transfer planning tools",
      authorizerConfiguration: agentcore.GatewayAuthorizer.usingAwsIam(),
      protocolConfiguration: agentcore.GatewayProtocol.mcp({
        supportedVersions: [agentcore.MCPProtocolVersion.of(MCP_VERSION)],
        instructions:
          "Expose corridors, quotes, transfer intent creation, and status only.",
      }),
      policyEngineConfiguration: {
        policyEngine,
        mode: agentcore.PolicyEngineMode.ENFORCE,
      },
    });
    gateway.role.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:GetWorkloadAccessToken"],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/${gatewayName}-??????????*`,
        ],
      }),
    );

    const stringType: agentcore.SchemaDefinition = {
      type: agentcore.SchemaDefinitionType.STRING,
    };
    const ownerProperty = {
      owner_sub: {
        type: agentcore.SchemaDefinitionType.STRING,
        description: "Trusted Cognito subject injected by the Runtime",
      },
    };
    const gatewayTools: GatewayTool[] = [
      {
        id: "ListCorridorsTool",
        targetName: "list-supported-corridors",
        toolName: "list_supported_corridors",
        description: "List the enabled demo Testnet corridors.",
        handler: "list_supported_corridors",
        access: "none",
        inputSchema: {
          type: agentcore.SchemaDefinitionType.OBJECT,
          properties: ownerProperty,
          required: ["owner_sub"],
        },
      },
      {
        id: "QuoteTool",
        targetName: "get-transfer-quote",
        toolName: "get_transfer_quote",
        description: "Create an expiring exact-output quote with bounded SendMax.",
        handler: "get_transfer_quote",
        access: "write",
        inputSchema: {
          type: agentcore.SchemaDefinitionType.OBJECT,
          properties: {
            ...ownerProperty,
            request: {
              type: agentcore.SchemaDefinitionType.OBJECT,
              properties: {
                corridor_id: stringType,
                destination_amount: stringType,
                payout_mode: stringType,
                recipient_address: stringType,
                destination_tag: {
                  type: agentcore.SchemaDefinitionType.INTEGER,
                },
                recipient_name: stringType,
                recipient_country: stringType,
                payout_alias: stringType,
                slippage_bps: {
                  type: agentcore.SchemaDefinitionType.INTEGER,
                },
              },
              required: [
                "corridor_id",
                "destination_amount",
                "payout_mode",
                "recipient_name",
              ],
            },
          },
          required: ["owner_sub", "request"],
        },
      },
      {
        id: "CreateIntentTool",
        targetName: "create-transfer-intent",
        toolName: "create_transfer_intent",
        description: "Create an unapproved transfer intent from a stored quote.",
        handler: "create_transfer_intent",
        access: "write",
        inputSchema: {
          type: agentcore.SchemaDefinitionType.OBJECT,
          properties: { ...ownerProperty, quote_id: stringType },
          required: ["owner_sub", "quote_id"],
        },
      },
      {
        id: "StatusTool",
        targetName: "get-transfer-status",
        toolName: "get_transfer_status",
        description: "Read the safe projection of an owned transfer.",
        handler: "get_transfer_status",
        access: "read",
        inputSchema: {
          type: agentcore.SchemaDefinitionType.OBJECT,
          properties: { ...ownerProperty, transfer_id: stringType },
          required: ["owner_sub", "transfer_id"],
        },
      },
    ];

    const gatewayTargetConstructs = new Map<string, Construct>();
    for (const definition of gatewayTools) {
      const fn = createFunction(
        `${definition.id}Function`,
        `xrpl_agentcore.gateway_tools.${definition.handler}`,
        commonEnvironment,
      );
      if (definition.access === "read") {
        transferTable.grantReadData(fn);
      } else if (definition.access === "write") {
        transferTable.grantReadWriteData(fn);
      }
      const target = gateway.addLambdaTarget(definition.id, {
        gatewayTargetName: definition.targetName,
        description: definition.description,
        lambdaFunction: fn,
        toolSchema: agentcore.ToolSchema.fromInline([
          {
            name: definition.toolName,
            description: definition.description,
            inputSchema: definition.inputSchema,
          },
        ]),
      });
      gatewayTargetConstructs.set(definition.id, target);
    }

    const runtimeRole = new iam.Role(this, "RuntimeRole", {
      roleName: "xrpl-agentcore-runtime-poc",
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: {
            "aws:SourceArn": `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*`,
          },
        },
      }),
      description:
        "AG-UI Runtime role with model, Gateway, and opt-in memory read access only",
    });
    const modelActions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ];
    const inferenceProfileArn = `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0`;
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: modelActions,
        resources: [inferenceProfileArn],
      }),
    );
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: modelActions,
        resources: ["us-east-1", "us-east-2", "us-west-2"].map(
          (region) =>
            `arn:aws:bedrock:${region}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0`,
        ),
        conditions: {
          StringEquals: {
            "bedrock:InferenceProfileArn": inferenceProfileArn,
          },
        },
      }),
    );
    gateway.grantInvoke(runtimeRole);
    preferenceMemory.grantReadLongTermMemory(runtimeRole);
    runtimeRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "kms:CreateGrant",
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:ReEncrypt*",
        ],
        resources: [memoryKey.keyArn],
        conditions: {
          StringEquals: {
            "kms:ViaService": `bedrock-agentcore.${this.region}.amazonaws.com`,
          },
        },
      }),
    );

    const runtimeRepository = new ecr.Repository(this, "RuntimeRepository", {
      repositoryName: "xrpl-agentcore-runtime",
      imageScanOnPush: true,
      encryption: ecr.RepositoryEncryption.KMS,
      encryptionKey: artifactKey,
      lifecycleRules: [{ maxImageCount: 5 }],
      removalPolicy: RemovalPolicy.DESTROY,
      emptyOnDelete: true,
    });
    runtimeRepository.grantPull(runtimeRole);

    // Remote build path for the Runtime image. Some environments (this POC's
    // laptop, a CI runner, a Cloud IDE) have no local container engine at
    // all. scripts/deploy.sh uploads a small source zip (Dockerfile.runtime,
    // pyproject.toml, README.md, src/ — exactly what .dockerignore allows
    // through) and CodeBuild does the ARM64 docker build and push instead.
    const runtimeBuildSource = new s3.Bucket(this, "RuntimeBuildSource", {
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [{ expiration: Duration.days(1) }],
    });
    const runtimeBuildProject = new codebuild.Project(
      this,
      "RuntimeBuildProject",
      {
        projectName: "xrpl-agentcore-runtime-build",
        source: codebuild.Source.s3({
          bucket: runtimeBuildSource,
          path: "runtime-build/source.zip",
        }),
        environment: {
          buildImage: codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
          computeType: codebuild.ComputeType.SMALL,
          privileged: true,
        },
        timeout: Duration.minutes(20),
        buildSpec: codebuild.BuildSpec.fromObject({
          version: "0.2",
          phases: {
            pre_build: {
              commands: [
                "aws ecr get-login-password --region $AWS_REGION | " +
                  'docker login --username AWS --password-stdin "${REPO_URI%%/*}"',
              ],
            },
            build: {
              commands: [
                "docker build --platform linux/arm64 --file Dockerfile.runtime " +
                  '--tag "${REPO_URI}:${IMAGE_TAG}" .',
              ],
            },
            post_build: {
              commands: ['docker push "${REPO_URI}:${IMAGE_TAG}"'],
            },
          },
        }),
      },
    );
    runtimeRepository.grantPullPush(runtimeBuildProject);

    const runtime = new agentcore.Runtime(this, "AgentRuntime", {
      runtimeName: "XrplTransferAssistant",
      description: "Native AG-UI Strands Runtime for the XRPL Testnet POC",
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromEcrRepository(
        runtimeRepository,
        props.runtimeImageTag ?? "latest",
      ),
      executionRole: runtimeRole,
      networkConfiguration:
        agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      protocolConfiguration: agentcore.ProtocolType.AGUI,
      authorizerConfiguration:
        agentcore.RuntimeAuthorizerConfiguration.usingCognito(
          userPool,
          [userPoolClient],
        ),
      requestHeaderConfiguration: {
        allowlistedHeaders: ["Authorization"],
      },
      environmentVariables: {
        AWS_DEFAULT_REGION: this.region,
        BEDROCK_MODEL_ID:
          "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        AGENTCORE_GATEWAY_URL: gateway.gatewayUrl ?? "",
        AGENTCORE_MCP_PROTOCOL_VERSION: MCP_VERSION,
        AGENTCORE_MEMORY_ID: preferenceMemory.memoryId,
        ALLOW_DEMO_AUTH: "false",
      },
      // Spans land in this Runtime's own log group
      // (/aws/bedrock-agentcore/runtimes/<id>-DEFAULT); metrics/traces need the
      // one-time account setup in scripts/enable_observability.sh first, or
      // the CloudWatch GenAI Observability dashboard stays empty regardless of
      // this flag. See docs/deployment.md#observability.
      tracingEnabled: true,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: Duration.minutes(15),
        maxLifetime: Duration.hours(8),
      },
    });
    const runtimeEndpoint = runtime.addEndpoint("default", {
      description: "Production-like Cognito-authorized AG-UI endpoint",
    });
    // tracingEnabled adds delivery resources referencing the Runtime's ARN, so
    // every CFN resource under it — the Runtime, its endpoint, and those
    // delivery resources alike — must share the same condition: none of them
    // can exist while the first bootstrap phase omits the Runtime itself.
    for (const child of runtime.node.findAll()) {
      if (child instanceof CfnResource) {
        child.cfnOptions.condition = deployAgentRuntimeCondition;
      }
    }
    // configureTracingDelivery also creates one stack-level X-Ray resource
    // policy, deduplicated by construct ID rather than scoped under the
    // Runtime — findAll() above never sees it, so it needs the same
    // condition applied separately. Its policy document embeds this
    // Runtime's ARN; safe only because this is the sole traced Runtime in
    // the stack — revisit if a second one is ever added here.
    const xrayDeliveryPolicy = this.node.tryFindChild(
      "CdkXRayLogsDeliveryPolicy",
    );
    for (const child of xrayDeliveryPolicy?.node.findAll() ?? []) {
      if (child instanceof CfnResource) {
        child.cfnOptions.condition = deployAgentRuntimeCondition;
      }
    }

    // Gateway and Memory don't have a tracingEnabled shortcut like Runtime —
    // AgentCore doesn't configure their log or trace destinations by default,
    // so both are wired up explicitly: one shared X-Ray destination (traces
    // aren't resource-specific), plus a dedicated CloudWatch log group each.
    // Same one-time account setup applies: see
    // scripts/enable_observability.sh and docs/deployment.md#observability.
    const tracesDestination = new logs.CfnDeliveryDestination(
      this,
      "TracesDestination",
      {
        name: `${this.stackName}-traces-destination`,
        deliveryDestinationType: "XRAY",
      },
    );
    const enableResourceObservability = (
      id: string,
      resourceArn: string,
      logGroup: logs.LogGroup,
    ): void => {
      const logsSource = new logs.CfnDeliverySource(this, `${id}LogsSource`, {
        name: `${this.stackName}-${id.toLowerCase()}-logs-source`,
        logType: "APPLICATION_LOGS",
        resourceArn,
      });
      const logsDestination = new logs.CfnDeliveryDestination(
        this,
        `${id}LogsDestination`,
        {
          name: `${this.stackName}-${id.toLowerCase()}-logs-destination`,
          deliveryDestinationType: "CWL",
          destinationResourceArn: logGroup.logGroupArn,
        },
      );
      const logsDelivery = new logs.CfnDelivery(this, `${id}LogsDelivery`, {
        deliverySourceName: logsSource.name,
        deliveryDestinationArn: logsDestination.attrArn,
      });
      // deliverySourceName is a plain string, not a Ref/GetAtt token, so CDK
      // can't infer this dependency on its own — without it, CloudFormation
      // may delete the source before the delivery that still points at it.
      logsDelivery.addResourceDependency(logsSource);
      logsDelivery.addResourceDependency(logsDestination);

      const tracesSource = new logs.CfnDeliverySource(
        this,
        `${id}TracesSource`,
        {
          name: `${this.stackName}-${id.toLowerCase()}-traces-source`,
          logType: "TRACES",
          resourceArn,
        },
      );
      const tracesDelivery = new logs.CfnDelivery(this, `${id}TracesDelivery`, {
        deliverySourceName: tracesSource.name,
        deliveryDestinationArn: tracesDestination.attrArn,
      });
      tracesDelivery.addResourceDependency(tracesSource);
      tracesDelivery.addResourceDependency(tracesDestination);
    };

    const gatewayLogGroup = new logs.LogGroup(this, "GatewayLogGroup", {
      logGroupName: `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/${gateway.gatewayId}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    enableResourceObservability("Gateway", gateway.gatewayArn, gatewayLogGroup);

    const memoryLogGroup = new logs.LogGroup(this, "MemoryLogGroup", {
      logGroupName: `/aws/vendedlogs/bedrock-agentcore/memory/APPLICATION_LOGS/${preferenceMemory.memoryId}`,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    enableResourceObservability(
      "Memory",
      preferenceMemory.memoryArn,
      memoryLogGroup,
    );

    const runtimePrincipal =
      `arn:aws:sts::${this.account}:assumed-role/${runtimeRole.roleName}`;
    for (const definition of gatewayTools) {
      const policy = policyEngine.addPolicy(`Permit${definition.id}`, {
        policyName: `Permit${definition.toolName.replaceAll("_", "")}`,
        description: `Permit only Runtime to call ${definition.toolName}`,
        validationMode: agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS,
        statement: agentcore.PolicyStatement.fromCedar(
          [
            "permit(",
            `  principal == AgentCore::IamEntity::"${runtimePrincipal}",`,
            `  action == AgentCore::Action::"${definition.targetName}___${definition.toolName}",`,
            `  resource == AgentCore::Gateway::"${gateway.gatewayArn}"`,
            ");",
          ].join("\n"),
        ),
      });
      policy.node.addDependency(
        gatewayTargetConstructs.get(definition.id) as Construct,
      );
    }

    new CfnOutput(this, "ApiUrl", { value: httpApi.apiEndpoint });
    new CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
    });
    new CfnOutput(this, "TransferTableName", {
      value: transferTable.tableName,
    });
    new CfnOutput(this, "ExecutionArtifactTableName", {
      value: artifactTable.tableName,
    });
    new CfnOutput(this, "TransferStateMachineArn", {
      value: stateMachine.stateMachineArn,
    });
    new CfnOutput(this, "GatewayUrl", { value: gateway.gatewayUrl ?? "" });
    new CfnOutput(this, "RuntimeRepositoryUri", {
      value: runtimeRepository.repositoryUri,
    });
    new CfnOutput(this, "RuntimeBuildProjectName", {
      value: runtimeBuildProject.projectName,
    });
    new CfnOutput(this, "RuntimeBuildSourceBucket", {
      value: runtimeBuildSource.bucketName,
    });
    const runtimeArnOutput = new CfnOutput(this, "RuntimeArn", {
      value: runtime.agentRuntimeArn,
    });
    runtimeArnOutput.condition = deployAgentRuntimeCondition;
    const runtimeEndpointOutput = new CfnOutput(this, "RuntimeEndpointArn", {
      value: runtimeEndpoint.agentRuntimeEndpointArn,
    });
    runtimeEndpointOutput.condition = deployAgentRuntimeCondition;
    new CfnOutput(this, "PreferenceMemoryId", {
      value: preferenceMemory.memoryId,
    });
    new CfnOutput(this, "GatewayLogGroupName", {
      value: gatewayLogGroup.logGroupName,
    });
    new CfnOutput(this, "MemoryLogGroupName", {
      value: memoryLogGroup.logGroupName,
    });
  }
}
