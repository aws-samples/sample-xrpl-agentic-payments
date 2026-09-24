import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";
import { XrplAgentCoreStack } from "../lib/xrpl-agentcore-stack";

let cachedTemplate: Record<string, any> | undefined;

function synthesized(): Record<string, any> {
  if (cachedTemplate) return cachedTemplate;
  const app = new App();
  const stack = new XrplAgentCoreStack(app, "TestStack", {
    env: { account: "111122223333", region: "us-west-2" },
  });
  cachedTemplate = Template.fromStack(stack).toJSON();
  return cachedTemplate;
}

function statementsForRole(
  template: Record<string, any>,
  roleLogicalId: string,
): Record<string, any>[] {
  const statements: Record<string, any>[] = [];
  const role = template.Resources[roleLogicalId];
  for (const policy of role.Properties.Policies ?? []) {
    statements.push(...policy.PolicyDocument.Statement);
  }
  for (const resource of Object.values<any>(template.Resources)) {
    if (resource.Type !== "AWS::IAM::Policy") continue;
    const roles = resource.Properties.Roles ?? [];
    if (roles.some((value: any) => value.Ref === roleLogicalId)) {
      statements.push(...resource.Properties.PolicyDocument.Statement);
    }
  }
  return statements;
}

function actions(statements: Record<string, any>[]): string[] {
  return statements.flatMap((statement) => {
    const action = statement.Action ?? [];
    return Array.isArray(action) ? action : [action];
  });
}

describe("XrplAgentCoreStack", () => {
  it("separates transfer and signed-artifact storage with customer KMS keys", () => {
    const template = Template.fromJSON(synthesized());
    template.resourceCountIs("AWS::DynamoDB::Table", 2);
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      KeySchema: Match.arrayWith([
        { AttributeName: "transfer_id", KeyType: "HASH" },
        { AttributeName: "kind", KeyType: "RANGE" },
      ]),
      SSESpecification: {
        KMSMasterKeyId: Match.anyValue(),
        SSEEnabled: true,
        SSEType: "KMS",
      },
    });
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      StreamSpecification: { StreamViewType: "NEW_IMAGE" },
      TimeToLiveSpecification: {
        AttributeName: "expires_at",
        Enabled: true,
      },
    });
  });

  it("deploys Cognito-authorized native AGUI Runtime and enforced Gateway Policy", () => {
    const template = Template.fromJSON(synthesized());
    template.resourceCountIs("AWS::BedrockAgentCore::Runtime", 1);
    template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      ProtocolConfiguration: "AGUI",
      AuthorizerConfiguration: {
        CustomJWTAuthorizer: Match.objectLike({
          AllowedClients: Match.anyValue(),
          DiscoveryUrl: Match.anyValue(),
        }),
      },
      RequestHeaderConfiguration: {
        RequestHeaderAllowlist: ["Authorization"],
      },
      EnvironmentVariables: Match.objectLike({
        XRPL_NETWORK: Match.absent(),
        TRANSFER_TABLE_NAME: Match.absent(),
        AGENTCORE_MEMORY_ID: Match.anyValue(),
      }),
    });
    template.resourceCountIs("AWS::BedrockAgentCore::GatewayTarget", 4);
    expect(JSON.stringify(synthesized())).toContain('"destination_tag"');
    template.resourceCountIs("AWS::BedrockAgentCore::PolicyEngine", 1);
    template.hasResourceProperties("AWS::BedrockAgentCore::Gateway", {
      AuthorizerType: "AWS_IAM",
      PolicyEngineConfiguration: {
        Arn: Match.anyValue(),
        Mode: "ENFORCE",
      },
    });
    // 4 Runtime-to-tool permits, plus one deliberate, narrow exception:
    // the registry-discovery demo consumer role may call
    // list_supported_corridors only. See docs/architecture.md#agent-registry.
    template.resourceCountIs("AWS::BedrockAgentCore::Policy", 5);

    const resources = synthesized().Resources;
    const targets = Object.entries<any>(resources)
      .filter(([, value]) => value.Type === "AWS::BedrockAgentCore::GatewayTarget")
      .map(([logicalId]) => logicalId);
    const policies = Object.values<any>(resources).filter(
      (value) => value.Type === "AWS::BedrockAgentCore::Policy",
    );
    for (const policy of policies) {
      expect(policy.DependsOn).toBeDefined();
      expect(
        targets.some((targetLogicalId) =>
          (Array.isArray(policy.DependsOn)
            ? policy.DependsOn
            : [policy.DependsOn]
          ).includes(targetLogicalId),
        ),
      ).toBe(true);
    }

    const cedarStatements = policies.map((policy) =>
      JSON.stringify(policy.Properties.Definition.Cedar.Statement),
    );
    const demoConsumerStatements = cedarStatements.filter((statement) =>
      statement.includes("RegistryConsumerDemoRole"),
    );
    expect(demoConsumerStatements).toHaveLength(1);
    expect(demoConsumerStatements[0]).toContain(
      "list-supported-corridors___list_supported_corridors",
    );
    for (const statement of cedarStatements) {
      if (statement.includes("RegistryConsumerDemoRole")) continue;
      // Every other permit stays scoped to the Runtime role only.
      expect(statement).not.toContain("RegistryConsumerDemoRole");
    }
  });

  it("allows both supported loopback browser origins", () => {
    const template = Template.fromJSON(synthesized());
    template.hasResourceProperties("AWS::ApiGatewayV2::Api", {
      CorsConfiguration: {
        AllowHeaders: [
          "authorization",
          "content-type",
          "idempotency-key",
          "x-memory-session",
        ],
        AllowMethods: ["GET", "POST", "OPTIONS"],
        AllowOrigins: [
          "http://localhost:3000",
          "http://127.0.0.1:3000",
        ],
      },
    });
    template.hasResourceProperties("AWS::Lambda::Function", {
      Handler: "xrpl_agentcore.api.lambda_handler",
      Environment: {
        Variables: Match.objectLike({
          CORS_ORIGINS:
            "http://localhost:3000,http://127.0.0.1:3000",
        }),
      },
    });
  });

  it("allows the Gateway to mint only its policy-session workload token", () => {
    const template = synthesized();
    const gatewayRoleId = Object.entries<any>(template.Resources).find(
      ([logicalId, value]) =>
        value.Type === "AWS::IAM::Role" &&
        logicalId.startsWith("GatewayServiceRole"),
    )?.[0];
    expect(gatewayRoleId).toBeTruthy();
    const tokenStatement = statementsForRole(
      template,
      gatewayRoleId as string,
    ).find((statement) =>
      (Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action]
      ).includes("bedrock-agentcore:GetWorkloadAccessToken"),
    );

    expect(tokenStatement).toBeDefined();
    expect(tokenStatement?.Resource).toEqual(
      expect.arrayContaining([
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:workload-identity-directory/default/workload-identity/xrpl-transfer-gateway-??????????*",
      ]),
    );
    expect(JSON.stringify(tokenStatement?.Resource)).toContain(
      "workload-identity-directory/default/workload-identity/",
    );
  });

  it("gives Runtime no table, workflow, Lambda, or signing-secret access", () => {
    const template = synthesized();
    const runtimeRoleId = Object.entries<any>(template.Resources).find(
      ([, value]) =>
        value.Type === "AWS::IAM::Role" &&
        value.Properties.RoleName === "xrpl-agentcore-runtime-poc",
    )?.[0];
    expect(runtimeRoleId).toBeTruthy();
    const runtimeActions = actions(
      statementsForRole(template, runtimeRoleId as string),
    );
    expect(runtimeActions).toContain("bedrock-agentcore:InvokeGateway");
    expect(runtimeActions).toContain(
      "bedrock-agentcore:RetrieveMemoryRecords",
    );
    expect(
      runtimeActions.some((action) =>
        /dynamodb|secretsmanager|states:StartExecution|lambda:InvokeFunction/.test(
          action,
        ),
      ),
    ).toBe(false);
  });

  it("limits memory key use to API writes and Runtime reads", () => {
    const template = synthesized();
    const apiFunction = Object.entries<any>(template.Resources).find(
      ([, value]) =>
        value.Type === "AWS::Lambda::Function" &&
        value.Properties.Handler === "xrpl_agentcore.api.lambda_handler",
    );
    const runtimeRoleId = Object.entries<any>(template.Resources).find(
      ([, value]) =>
        value.Type === "AWS::IAM::Role" &&
        value.Properties.RoleName === "xrpl-agentcore-runtime-poc",
    )?.[0];
    expect(apiFunction).toBeTruthy();
    expect(runtimeRoleId).toBeTruthy();

    const apiRoleId = apiFunction?.[1].Properties.Role["Fn::GetAtt"][0];
    const apiKmsActions = actions(statementsForRole(template, apiRoleId)).filter(
      (action) => action.startsWith("kms:"),
    );
    const runtimeKmsActions = actions(
      statementsForRole(template, runtimeRoleId as string),
    ).filter((action) => action.startsWith("kms:"));
    const runtimeMemoryKmsStatement = statementsForRole(
      template,
      runtimeRoleId as string,
    ).find((statement) =>
      (Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action]
      ).includes("kms:CreateGrant"),
    );

    expect(apiKmsActions).toEqual(
      expect.arrayContaining([
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
      ]),
    );
    expect(apiKmsActions).not.toContain("kms:*");
    expect(runtimeKmsActions).toEqual(
      expect.arrayContaining([
        "kms:CreateGrant",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:ReEncrypt*",
      ]),
    );
    expect(runtimeKmsActions).not.toContain("kms:*");
    expect(runtimeMemoryKmsStatement?.Condition).toEqual({
      StringEquals: {
        "kms:ViaService":
          "bedrock-agentcore.us-west-2.amazonaws.com",
      },
    });
  });

  it("permits only the US Sonnet profile and its routed model regions", () => {
    const template = synthesized();
    const runtimeRoleId = Object.entries<any>(template.Resources).find(
      ([, value]) =>
        value.Type === "AWS::IAM::Role" &&
        value.Properties.RoleName === "xrpl-agentcore-runtime-poc",
    )?.[0];
    expect(runtimeRoleId).toBeTruthy();
    const modelStatements = statementsForRole(
      template,
      runtimeRoleId as string,
    ).filter((statement) =>
      (Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action]
      ).includes("bedrock:InvokeModelWithResponseStream"),
    );
    const resources = modelStatements.flatMap((statement) =>
      Array.isArray(statement.Resource)
        ? statement.Resource
        : [statement.Resource],
    );

    expect(resources).toEqual(
      expect.arrayContaining([
        "arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
      ]),
    );
    const routedModels = modelStatements.find(
      (statement) => statement.Condition?.StringEquals,
    );
    expect(routedModels?.Condition.StringEquals).toEqual({
      "bedrock:InferenceProfileArn":
        "arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    });
  });

  it("limits seeds to signer and gives reconciler read-only artifact access", () => {
    const template = synthesized();
    const functions = Object.entries<any>(template.Resources).filter(
      ([, value]) => value.Type === "AWS::Lambda::Function",
    );
    const signer = functions.find(
      ([, value]) =>
        value.Properties.Environment?.Variables?.EXECUTION_ROLE_MODE ===
        "SIGNER",
    );
    const reconciler = functions.find(
      ([, value]) =>
        value.Properties.Environment?.Variables?.EXECUTION_ROLE_MODE ===
        "RECONCILER",
    );
    expect(signer).toBeTruthy();
    expect(reconciler).toBeTruthy();

    const signerRoleId = signer?.[1].Properties.Role["Fn::GetAtt"][0];
    const reconcilerRoleId = reconciler?.[1].Properties.Role["Fn::GetAtt"][0];
    const signerActions = actions(statementsForRole(template, signerRoleId));
    const reconcilerStatements = statementsForRole(template, reconcilerRoleId);
    const reconcilerActions = actions(reconcilerStatements);
    const artifactTableId = Object.entries<any>(template.Resources).find(
      ([, value]) =>
        value.Type === "AWS::DynamoDB::Table" &&
        value.Properties.KeySchema?.some(
          (key: any) => key.AttributeName === "transfer_id",
        ),
    )?.[0];
    const reconcilerArtifactActions = actions(
      reconcilerStatements.filter((statement) =>
        JSON.stringify(statement.Resource).includes(artifactTableId as string),
      ),
    );

    expect(signerActions).toContain("secretsmanager:GetSecretValue");
    expect(signerActions).toContain("dynamodb:PutItem");
    expect(reconcilerActions).toContain("dynamodb:GetItem");
    expect(reconcilerArtifactActions).not.toContain("dynamodb:PutItem");
    expect(
      reconcilerActions.some((action) => action.startsWith("secretsmanager:")),
    ).toBe(false);
  });

  it("has a Testnet-only durable workflow and no approval or execution tool", () => {
    const raw = JSON.stringify(synthesized());
    expect(raw).toContain("https://s.altnet.rippletest.net:51234");
    expect(raw).toContain('"XRPL_NETWORK":"testnet"');
    expect(raw).toContain('"XRPL_USD_ISSUER_ADDRESS"');
    expect(raw).toContain('"XRPL_MXN_ISSUER_ADDRESS"');
    expect(raw).not.toContain('___approve_transfer"');
    expect(raw).not.toContain('___execute_transfer"');
    const workflow = Object.values<any>(synthesized().Resources).find(
      (resource) => resource.Type === "AWS::StepFunctions::StateMachine",
    );
    const definition = workflow.Properties.DefinitionString["Fn::Join"][1]
      .filter((fragment: unknown) => typeof fragment === "string")
      .join("");
    expect(definition).not.toContain('"BooleanEquals":false');
    for (const field of ["done", "terminal", "refund"]) {
      expect(definition).toContain(
        `"Variable":"$.result.${field}","IsPresent":true`,
      );
      expect(definition).toContain(
        `"Variable":"$.result.${field}","BooleanEquals":true`,
      );
    }
    const template = Template.fromJSON(synthesized());
    template.resourceCountIs("AWS::StepFunctions::StateMachine", 1);
    template.hasResourceProperties("AWS::Lambda::Function", {
      ReservedConcurrentExecutions: 1,
      Environment: {
        Variables: Match.objectLike({
          EXECUTION_ROLE_MODE: "SIGNER",
        }),
      },
    });
  });
});
