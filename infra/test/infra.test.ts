/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/**
 * Regression tests for the failures this app has actually shipped.
 *
 * Every assertion here corresponds to a bug that deployed green and broke at
 * runtime: an unset Lambda environment variable, a Karpenter selector that
 * matched nothing, a layer S3 key nothing produced, and a public plaintext
 * login page. They are written against the synthesized template (and, for the
 * build-script contract, against the file on disk) because that is the only
 * place those mismatches are visible before a deploy.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import {
  XrplAgenticPaymentsToolsStack,
  XRPL_LAYER_ZIP_FILENAME,
  MCP_PROTOCOL_VERSION,
  GATEWAY_TOOL_SEPARATOR,
} from "../lib/tools-stack";
import {
  XrplAgenticPaymentsEksStack,
  KARPENTER_DISCOVERY_TAG,
  KARPENTER_DISCOVERY_VALUE,
} from "../lib/eks-stack";
import { XrplAgenticPaymentsWebStack } from "../lib/web-stack";
import {
  XrplAgenticPaymentsStack,
  WEBAPP_ECR_REPO_NAME,
} from "../lib/infra-stack";

// Synthesizing an EKS cluster (plus its CDK-internal custom-resource providers)
// is well past Jest's 5s default.
jest.setTimeout(180_000);

const ENV = { account: "123456789012", region: "us-west-2" };

/**
 * An App carrying the same context the CLI would pass — i.e. cdk.json.
 *
 * A bare `new cdk.App()` under Jest reads context from CDK_CONTEXT_JSON and
 * cdk.context.json, and not from cdk.json: loading that file is the CLI's job,
 * and the CLI is not in the room. So a bare App in a test synthesizes with
 * every feature flag at its *library default*, which for a project pinned to
 * an older-than-current aws-cdk-lib is a different template from the one that
 * deploys. The whole suite was asserting on a template no `cdk deploy` ever
 * produces — @aws-cdk/aws-lambda:useCdkManagedLogGroup alone is the difference
 * between nine AWS::Logs::LogGroup resources and zero.
 *
 * Read from disk rather than duplicated here so the two cannot drift.
 */
function newApp(): cdk.App {
  const { context } = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../cdk.json"), "utf8")
  );
  return new cdk.App({ context });
}

const DOMAIN = {
  domainName: "example.com",
  subdomain: "xrpl-agentic-payments",
  certificateArn:
    "arn:aws:acm:us-west-2:123456789012:certificate/11111111-2222-3333-4444-555555555555",
};

// Public XRPL addresses, which is why they can sit in a test file at all.
const XRPL_ADDRESSES = {
  executionAddress: "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg",
  rlusdIssuer: "rJ6VE6L87yaVmdyxa9jZFXQpbThqbncQry",
};

// Stands in for the ToolsStack outputs the app wires into the EKS stack. Values
// are shaped like the real ones so the assertions below fail on a placeholder.
const GATEWAY = {
  url: "https://xrpl-agentic-payments-abc123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
  id: "xrpl-agentic-payments-abc123",
  arn: "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/xrpl-agentic-payments-abc123",
};

const REPO_ROOT = path.join(__dirname, "../..");

/** Minimal valid (empty) zip: enough for CDK to stage an asset. */
const EMPTY_ZIP = Buffer.from(
  "504b0506000000000000000000000000000000000000",
  "hex"
);

/**
 * Manifest properties render as a plain string when the manifest contains no
 * tokens and as an Fn::Join otherwise. Flattening both to one string lets a
 * test assert on the Kubernetes object without caring which form CDK chose.
 */
function flattenManifest(value: any): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(flattenManifest).join("");
  if (value && typeof value === "object") {
    return Object.values(value).map(flattenManifest).join("");
  }
  return "";
}

function k8sManifests(template: Template): string[] {
  const resources = template.findResources(
    "Custom::AWSCDK-EKS-KubernetesResource"
  );
  return Object.values(resources).map((r: any) =>
    flattenManifest(r.Properties?.Manifest)
  );
}

/**
 * Finds the manifest whose OWN kind is `kind`. The `,"metadata"` suffix matters:
 * a bare `"kind":"Deployment"` also appears inside the HPA's scaleTargetRef and
 * `"kind":"EC2NodeClass"` inside the NodePool's nodeClassRef, so matching the
 * kind alone can return the wrong object.
 */
function manifestOfKind(template: Template, kind: string): string {
  const found = k8sManifests(template).filter((m) =>
    m.includes(`"kind":"${kind}","metadata"`)
  );
  expect(found).toHaveLength(1);
  return found[0];
}

// ═══════════════════════════════════════════════════════════════════════
// Tools stack — Lambda tools, shared layer, OFAC dataset
// ═══════════════════════════════════════════════════════════════════════

describe("XrplAgenticPaymentsToolsStack", () => {
  let template: Template;
  let layerZipPath: string;

  beforeAll(() => {
    // A real layer archive is a cross-compiled ARM64 build, so the test stages
    // a throwaway zip instead of requiring one in the working tree.
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "xrpl-layer-"));
    layerZipPath = path.join(dir, XRPL_LAYER_ZIP_FILENAME);
    fs.writeFileSync(layerZipPath, EMPTY_ZIP);

    const app = newApp();
    const stack = new XrplAgenticPaymentsToolsStack(app, "TestToolsStack", {
      env: ENV,
      xrplLayerZipPath: layerZipPath,
    });
    template = Template.fromStack(stack);
  });

  test("screen_sanctions Lambda has ARTIFACTS_BUCKET set", () => {
    // Unset, screen_sanctions.py resolved the bucket name
    // "xrpl-agentic-payments-artifacts-" (AWS_ACCOUNT_ID is never set in
    // Lambda) and every cold start died with NoSuchBucket.
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "xrpl-agentic-payments-screen-sanctions",
      Environment: {
        Variables: Match.objectLike({
          ARTIFACTS_BUCKET: `xrpl-agentic-payments-artifacts-${ENV.account}`,
        }),
      },
    });
  });

  test("the OFAC object key the stack uploads is the one the Lambda reads", () => {
    const lambdaSource = fs.readFileSync(
      path.join(REPO_ROOT, "functions/screen_sanctions.py"),
      "utf8"
    );
    expect(lambdaSource).toContain('S3_KEY = "data/ofac_sdn.csv"');
    // The BucketDeployment writes under this prefix; the key above is
    // <prefix>/ofac_sdn.csv.
    template.hasResourceProperties("Custom::CDKBucketDeployment", {
      DestinationBucketKeyPrefix: "data",
    });
  });

  test("the layer is a CDK asset, not a hand-uploaded S3 object", () => {
    // Selected by name rather than asserted to be the only layer in the stack.
    // It is not: the OfacSdnDataset BucketDeployment above brings CDK's own
    // AwsCliLayer with it, so a `toHaveLength(1)` here fails on a stack that is
    // perfectly correct — which is what it was doing.
    const layers = template.findResources("AWS::Lambda::LayerVersion", {
      Properties: { LayerName: "xrpl-agentic-payments-xrpl-layer" },
    });
    const contents = Object.values(layers).map(
      (l: any) => l.Properties.Content
    );
    expect(contents).toHaveLength(1);
    expect(contents[0].S3Key).toMatch(/\.zip$/);
    // The old code read layers/xrpl-layer-v1.zip out of the artifacts bucket,
    // an object no build step ever produced.
    expect(JSON.stringify(contents[0])).not.toContain(
      "xrpl-agentic-payments-artifacts"
    );
  });

  test("the layer archive name matches what build_layer.sh produces", () => {
    const script = fs.readFileSync(
      path.join(REPO_ROOT, "layers/xrpl/build_layer.sh"),
      "utf8"
    );
    expect(script).toContain(`OUTPUT="$SCRIPT_DIR/${XRPL_LAYER_ZIP_FILENAME}"`);
  });

  test("synth fails with a build hint when the layer has not been built", () => {
    expect(
      () =>
        new XrplAgenticPaymentsToolsStack(newApp(), "TestToolsStackNoZip", {
          env: ENV,
          xrplLayerZipPath: path.join(
            os.tmpdir(),
            "xrpl-layer-does-not-exist.zip"
          ),
        })
    ).toThrow(/build_layer\.sh/);
  });

  test("all eight tool Lambdas share the layer", () => {
    const tools = Object.values(
      template.findResources("AWS::Lambda::Function")
    ).filter((fn: any) =>
      String(fn.Properties?.FunctionName ?? "").startsWith(
        "xrpl-agentic-payments-"
      )
    );
    expect(tools).toHaveLength(8);
    for (const fn of tools as any[]) {
      expect(fn.Properties.Layers).toHaveLength(1);
      expect(fn.Properties.Architectures).toEqual(["arm64"]);
    }
  });

  test("no log group is retained when the stack goes away", () => {
    // useCdkManagedLogGroup (cdk.json) makes every Function emit its own
    // AWS::Logs::LogGroup, and CDK's LogGroup defaults to DeletionPolicy:
    // Retain. The tool Lambdas have fixed functionNames, so the group names
    // are fixed too — a create that rolls back orphans all of them and the
    // next attempt dies on
    //   /aws/lambda/xrpl-agentic-payments-get-balance already exists
    // against a log group holding zero bytes. It took two manual sweeps
    // (7 orphans, then 8) to get this stack deployed the first time.
    //
    // Asserted over every log group in the template, not just the eight
    // named ones: the ninth belongs to the BucketDeployment handler CDK
    // synthesizes for OfacSdnDataset, and it blocks a retry exactly the same
    // way. That is why the fix is RemovalPolicies over the stack rather than
    // a prop on each Function — a prop cannot reach a construct this stack
    // does not create.
    const logGroups = Object.values(
      template.findResources("AWS::Logs::LogGroup")
    );
    expect(logGroups.length).toBeGreaterThanOrEqual(9);

    for (const group of logGroups as any[]) {
      expect(group.DeletionPolicy).toBe("Delete");
      expect(group.UpdateReplacePolicy).toBe("Delete");
      // Retention is what ages logs out, and it is not what was changed.
      // Deleting the group with the stack while keeping logs forever inside
      // it is the pair that makes sense; losing retention would not.
      expect(group.Properties.RetentionInDays).toBe(731);
    }
  });

  // ─────────────────────────────────────────────────────────────────────
  // The Gateway — what makes the eight Lambdas reachable at all
  // ─────────────────────────────────────────────────────────────────────

  describe("the Gateway in front of the tool Lambdas", () => {
    /** Logical ID → FunctionName, for resolving the GetAtt in each target. */
    let toolLambdas: Record<string, string>;
    /** Target name → the target's synthesized properties. */
    let targets: Record<string, any>;

    beforeAll(() => {
      toolLambdas = Object.fromEntries(
        Object.entries(template.findResources("AWS::Lambda::Function"))
          .map(([id, fn]: [string, any]) => [
            id,
            String(fn.Properties?.FunctionName ?? ""),
          ])
          .filter(([, name]) => name.startsWith("xrpl-agentic-payments-"))
      );
      targets = Object.fromEntries(
        Object.values(
          template.findResources("AWS::BedrockAgentCore::GatewayTarget")
        ).map((t: any) => [t.Properties.Name, t.Properties])
      );
    });

    test("the stack actually creates a Gateway", () => {
      // It did not. The Gateway was in every architecture diagram, in the
      // health check and in a metrics tile; the only thing the stack did about
      // it was grant bedrock-agentcore permission to invoke the Lambdas. So the
      // orchestrator called the Runtime-hosted MCP server instead and these
      // eight functions were deployed and unreachable.
      template.resourceCountIs("AWS::BedrockAgentCore::Gateway", 1);
      template.hasResourceProperties("AWS::BedrockAgentCore::Gateway", {
        ProtocolType: "MCP",
        // SigV4. With CUSTOM_JWT the pod would need an OAuth issuer and a token
        // it has no way to get; it already has an AWS identity via Pod Identity.
        AuthorizerType: "AWS_IAM",
      });
    });

    test("the Gateway accepts the protocol version the pod is told to send", () => {
      // Both sides come from MCP_PROTOCOL_VERSION — the gateway here, the pod's
      // AGENTCORE_MCP_PROTOCOL_VERSION in the EKS stack. A gateway refuses any
      // version outside supportedVersions, so a divergence is a 400 on the
      // first tool call after a deploy and nothing earlier.
      template.hasResourceProperties("AWS::BedrockAgentCore::Gateway", {
        ProtocolConfiguration: {
          Mcp: { SupportedVersions: [MCP_PROTOCOL_VERSION] },
        },
      });
    });

    test("every tool Lambda is fronted by exactly one target", () => {
      // The point of the whole change: a Lambda with no target is a tool the
      // Gateway cannot route to, and it deploys green.
      const fronted = Object.values(targets).map(
        (p: any) =>
          toolLambdas[p.TargetConfiguration.Mcp.Lambda.LambdaArn["Fn::GetAtt"][0]]
      );

      expect(fronted.filter(Boolean)).toHaveLength(8);
      expect(new Set(fronted).size).toBe(8);
      expect(new Set(fronted)).toEqual(new Set(Object.values(toolLambdas)));
    });

    test("target names can never be mistaken for part of a tool name", () => {
      // Gateway exposes each tool as `<targetName>___<toolName>` and
      // strip_tool_prefix (functions/shared.py) splits on the FIRST separator.
      // A target name containing one would silently resolve to a different
      // tool. The API's own charset forbids it; asserted because the failure is
      // silent misrouting rather than an error.
      for (const name of Object.keys(targets)) {
        expect(name).not.toContain(GATEWAY_TOOL_SEPARATOR);
        expect(name).toMatch(/^[0-9a-zA-Z][0-9a-zA-Z-]{0,99}$/);
      }
    });

    test("each target registers one tool with a typed input schema", () => {
      for (const [name, props] of Object.entries(targets)) {
        const payload = props.TargetConfiguration.Mcp.Lambda.ToolSchema
          .InlinePayload;
        expect(payload).toHaveLength(1);

        const tool = payload[0];
        // The Gateway rejects a call whose arguments do not match this schema,
        // so an empty properties bag makes the tool uncallable with arguments.
        expect(tool.InputSchema.Type).toBe("object");
        expect(Object.keys(tool.InputSchema.Properties).length).toBeGreaterThan(0);
        for (const prop of Object.values<any>(tool.InputSchema.Properties)) {
          expect(prop.Description).toBeTruthy();
          expect([
            "string",
            "number",
            "integer",
            "boolean",
            "object",
            "array",
          ]).toContain(prop.Type);
        }
        // The bare snake_case handler name; the Gateway adds the prefix.
        expect(tool.Name).toMatch(/^[a-z][a-z_]+$/);
        expect(name).toBe(tool.Name.replace(/_/g, "-"));
      }
    });

    test("only submit-payment takes the session id that reaches the ledger", () => {
      // submit_payment writes session_id into the XRPL attribution memo, so the
      // Gateway has to accept it as an argument — src/agentcore_client.py
      // injects it. Everywhere else it would be an argument the schema does not
      // declare, which the Gateway rejects, so the client must not send it.
      const declares = Object.entries(targets)
        .filter(([, p]: [string, any]) =>
          Object.keys(
            p.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload[0]
              .InputSchema.Properties
          ).includes("session_id")
        )
        .map(([name]) => name);

      expect(declares).toEqual(["submit-payment"]);
    });

    test("the Gateway invokes the Lambdas as itself, not as the whole service", () => {
      // Before: each Lambda carried a resource policy allowing
      // bedrock-agentcore.amazonaws.com to invoke it, which is any caller in
      // any account that can reach the service. Now a dedicated role holds an
      // identity policy, and the trust policy is confused-deputy conditioned.
      template.resourceCountIs("AWS::Lambda::Permission", 0);

      template.hasResourceProperties("AWS::IAM::Role", {
        RoleName: "xrpl-agentic-payments-gateway",
        AssumeRolePolicyDocument: {
          Statement: [
            Match.objectLike({
              Principal: { Service: "bedrock-agentcore.amazonaws.com" },
              Condition: {
                StringEquals: { "aws:SourceAccount": ENV.account },
                ArnLike: Match.anyValue(),
              },
            }),
          ],
        },
      });
    });

    test("every Lambda target declares the IAM-role credential provider", () => {
      // This one deploys or it does not: the AgentCore API rejects a Lambda
      // target with no CredentialProviderConfigurations —
      //   "Invalid request provided: CredentialProviderConfigurations is
      //    required for Lambda targets"
      // — while the CloudFormation reference marks the property *optional* on
      // the resource. So synth passed, cdk-nag passed, and all eight targets
      // failed at CREATE. Nothing but a deploy caught it, which is why it is
      // asserted here.
      //
      // GATEWAY_IAM_ROLE is the only provider Lambda targets accept, and it is
      // the one that matches the gateway role the grantInvoke() loop
      // authorizes; OAUTH/API_KEY apply to HTTP and OpenAPI targets.
      expect(Object.keys(targets)).toHaveLength(8);

      const missing = Object.entries(targets)
        .filter(
          ([, props]) =>
            JSON.stringify(props.CredentialProviderConfigurations) !==
            JSON.stringify([{ CredentialProviderType: "GATEWAY_IAM_ROLE" }])
        )
        .map(([name]) => name);

      expect(missing).toEqual([]);
    });
  });
});

// ═══════════════════════════════════════════════════════════════════════
// Web stack — TLS is mandatory
// ═══════════════════════════════════════════════════════════════════════

describe("XrplAgenticPaymentsWebStack", () => {
  test("synth fails instead of serving the login form over plaintext HTTP", () => {
    expect(
      () =>
        new XrplAgenticPaymentsWebStack(
          newApp(),
          "TestWebStackNoTls",
          { env: ENV } as any
        )
    ).toThrow(/TLS/);
  });

  describe("with a domain", () => {
    let template: Template;

    beforeAll(() => {
      const stack = new XrplAgenticPaymentsWebStack(
        newApp(),
        "TestWebStack",
        { env: ENV, domain: DOMAIN }
      );
      template = Template.fromStack(stack);
    });

    test("serves HTTPS on a policy that cannot negotiate TLS 1.0/1.1", () => {
      // Matches the TLS13-* and TLS-1-2-* policy families rather than one
      // literal, so a policy bump does not fail the test — but leaving
      // SslPolicy unset (the ALB default, ELBSecurityPolicy-2016-08, which
      // still accepts TLS 1.0) does.
      template.hasResourceProperties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
          Port: 443,
          Protocol: "HTTPS",
          SslPolicy: Match.stringLikeRegexp("ELBSecurityPolicy-(TLS13|TLS-1-2)"),
        }
      );
    });

    test("port 80 only redirects, permanently", () => {
      template.hasResourceProperties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {
          Port: 80,
          Protocol: "HTTP",
          DefaultActions: [
            Match.objectLike({
              Type: "redirect",
              RedirectConfig: Match.objectLike({
                Protocol: "HTTPS",
                Port: "443",
                StatusCode: "HTTP_301",
              }),
            }),
          ],
        }
      );
    });

    test("nothing forwards application traffic in cleartext", () => {
      const listeners = Object.values(
        template.findResources("AWS::ElasticLoadBalancingV2::Listener")
      ) as any[];
      expect(listeners).toHaveLength(2);
      const plaintextForward = listeners.filter(
        (l) =>
          l.Properties.Protocol === "HTTP" &&
          l.Properties.DefaultActions?.some((a: any) => a.Type === "forward")
      );
      expect(plaintextForward).toHaveLength(0);
    });
  });
});

// ═══════════════════════════════════════════════════════════════════════
// EKS stack — Karpenter discovery, TLS ingress, wallet secret mount
// ═══════════════════════════════════════════════════════════════════════

describe("XrplAgenticPaymentsEksStack", () => {
  test("synth fails instead of exposing the app over plaintext HTTP", () => {
    expect(
      () =>
        new XrplAgenticPaymentsEksStack(
          newApp(),
          "TestEksStackNoTls",
          { env: ENV } as any
        )
    ).toThrow(/TLS/);
  });

  describe("with a domain", () => {
    let template: Template;

    beforeAll(() => {
      const stack = new XrplAgenticPaymentsEksStack(
        newApp(),
        "TestEksStack",
        {
          env: ENV,
          domain: {
            hostname: `${DOMAIN.subdomain}.${DOMAIN.domainName}`,
            certificateArn: DOMAIN.certificateArn,
          },
          xrplAddresses: XRPL_ADDRESSES,
          gateway: GATEWAY,
        }
      );
      template = Template.fromStack(stack);
    });

    test("private subnets carry both tags the EC2NodeClass selects on", () => {
      // subnetSelectorTerms is a single term with TWO tags, so a subnet only
      // matches if it carries both. If either value diverges from the selector,
      // Karpenter resolves zero subnets and no node ever launches — while
      // `cdk deploy` still succeeds.
      template.hasResourceProperties("AWS::EC2::Subnet", {
        Tags: Match.arrayWith([
          { Key: KARPENTER_DISCOVERY_TAG, Value: KARPENTER_DISCOVERY_VALUE },
          { Key: "network", Value: "private" },
        ]),
      });
    });

    test("the Karpenter node security group carries the discovery tag", () => {
      template.hasResourceProperties("AWS::EC2::SecurityGroup", {
        GroupDescription: "XRPL Agentic Payments Karpenter worker nodes",
        Tags: Match.arrayWith([
          { Key: KARPENTER_DISCOVERY_TAG, Value: KARPENTER_DISCOVERY_VALUE },
        ]),
      });
    });

    test("EC2NodeClass selectors use the same discovery tag value as the subnets", () => {
      const nodeClass = manifestOfKind(template, "EC2NodeClass");
      const selector = `"${KARPENTER_DISCOVERY_TAG}":"${KARPENTER_DISCOVERY_VALUE}"`;
      // Once for subnetSelectorTerms, once for securityGroupSelectorTerms.
      expect(nodeClass.split(selector)).toHaveLength(3);
      expect(nodeClass).toContain('"network":"private"');
    });

    test("the NodePool points at that EC2NodeClass", () => {
      const nodePool = manifestOfKind(template, "NodePool");
      expect(nodePool).toContain('"kind":"EC2NodeClass"');
      expect(nodePool).toContain('"name":"xrpl-agentic-payments"');
    });

    test("the Ingress terminates TLS and redirects port 80", () => {
      const ingress = manifestOfKind(template, "Ingress");
      expect(ingress).toContain(
        '"alb.ingress.kubernetes.io/listen-ports":"[{\\"HTTPS\\":443},{\\"HTTP\\":80}]"'
      );
      expect(ingress).toContain(
        '"alb.ingress.kubernetes.io/ssl-redirect":"443"'
      );
      expect(ingress).toContain(
        '"alb.ingress.kubernetes.io/ssl-policy":"ELBSecurityPolicy-TLS13'
      );
      expect(ingress).toContain(DOMAIN.certificateArn);
      expect(ingress).toContain(`"host":"${DOMAIN.subdomain}.${DOMAIN.domainName}"`);
      // The pre-fix annotation, which produced an HTTP-only ALB.
      expect(ingress).not.toContain('listen-ports":"[{\\"HTTP\\":80}]"');
    });

    test("the web pod does not receive the wallet seeds at all", () => {
      // The inverse of what this test used to assert. The seeds WERE mounted
      // here, at /app/config/wallets.json, so the orchestrator could read two
      // public addresses out of the file that also holds every private key —
      // pulling the treasury and execution seeds into a long-lived
      // multi-tenant process that cannot spend them. Nothing in the web tier
      // signs; the Lambda tools and the AgentCore Runtime MCP server read the
      // secret themselves at invoke time.
      const spcs = k8sManifests(template).filter((m) =>
        m.includes('"kind":"SecretProviderClass"')
      );
      expect(
        spcs.find((m) => m.includes("xrpl-agentic-payments/wallets"))
      ).toBeUndefined();

      const deployment = manifestOfKind(template, "Deployment");
      expect(deployment).not.toContain("wallets.json");
      expect(deployment).not.toContain('"mountPath":"/app/config"');
    });

    test("the addresses that replaced the mount are plain env, not a secret ref", () => {
      // If these ever regress to a secretKeyRef off the wallets secret, the pod
      // is back to holding seeds. And if they vanish entirely the orchestrator
      // falls back to config/wallets.json, which is not in this image.
      const deployment = manifestOfKind(template, "Deployment");
      expect(deployment).toContain(
        `{"name":"XRPL_EXECUTION_ADDRESS","value":"${XRPL_ADDRESSES.executionAddress}"}`
      );
      expect(deployment).toContain(
        `{"name":"XRPL_RLUSD_ISSUER","value":"${XRPL_ADDRESSES.rlusdIssuer}"}`
      );
    });

    test("the pod is told where the Gateway is", () => {
      // src/agentcore_client.py has no default for this and refuses to call
      // anything without it — deliberately, since the alternative to erroring is
      // a fallback path that skips the Gateway. Unset, every tool call fails.
      const deployment = manifestOfKind(template, "Deployment");
      expect(deployment).toContain(
        `{"name":"AGENTCORE_GATEWAY_URL","value":"${GATEWAY.url}"}`
      );
      expect(deployment).toContain(
        `{"name":"AGENTCORE_GATEWAY_ID","value":"${GATEWAY.id}"}`
      );
      expect(deployment).toContain(
        `{"name":"AGENTCORE_MCP_PROTOCOL_VERSION","value":"${MCP_PROTOCOL_VERSION}"}`
      );
    });

    test("the pod may invoke that Gateway and only that Gateway", () => {
      // The grant used to be gateway/*, which was every gateway in the account
      // — including any a different workload creates later.
      template.hasResourceProperties("AWS::IAM::Policy", {
        PolicyDocument: {
          Statement: Match.arrayWith([
            {
              Sid: "AgentCoreGateway",
              Action: "bedrock-agentcore:InvokeGateway",
              Effect: "Allow",
              Resource: GATEWAY.arn,
            },
          ]),
        },
      });

      const policies = JSON.stringify(
        template.findResources("AWS::IAM::Policy")
      );
      expect(policies).not.toContain(":gateway/*");
    });
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// The web app image: built for arm64, and pulled from a repository this app
// actually creates.
//
// eks-stack.ts names the image as a plain URI string rather than referencing the
// Repository object, so CDK itself does not connect the two. Nothing would have
// caught a renamed repository — the cluster would come up and the pods would sit
// in ImagePullBackOff. These tests are that connection.
// ═══════════════════════════════════════════════════════════════════════════

describe("the web app container image", () => {
  let coreTemplate: Template;
  let eksTemplate: Template;

  beforeAll(() => {
    coreTemplate = Template.fromStack(
      new XrplAgenticPaymentsStack(newApp(), "TestCoreStack", { env: ENV })
    );

    const eksApp = newApp();
    eksTemplate = Template.fromStack(
      new XrplAgenticPaymentsEksStack(eksApp, "TestEksStackImage", {
        env: ENV,
        domain: {
          hostname: `${DOMAIN.subdomain}.${DOMAIN.domainName}`,
          certificateArn: DOMAIN.certificateArn,
        },
        xrplAddresses: XRPL_ADDRESSES,
        gateway: GATEWAY,
      } as any)
    );
  });

  test("the core stack creates the repository the EKS pod pulls from", () => {
    coreTemplate.hasResourceProperties("AWS::ECR::Repository", {
      RepositoryName: WEBAPP_ECR_REPO_NAME,
    });

    // The EKS manifest embeds the image URI inside a Kubernetes manifest body,
    // so this reads the whole template rather than one property path.
    expect(JSON.stringify(eksTemplate.toJSON())).toContain(
      `/${WEBAPP_ECR_REPO_NAME}:`
    );
  });

  test("the EKS pod does not pull from a repository nothing creates", () => {
    const created = Object.values(
      coreTemplate.findResources("AWS::ECR::Repository")
    ).map((r: any) => r.Properties.RepositoryName);

    const eksJson = JSON.stringify(eksTemplate.toJSON());
    const pulled = [
      ...eksJson.matchAll(/dkr\.ecr\.[^/"]+\/([a-z0-9][a-z0-9._/-]*):/g),
    ].map((m) => m[1]);

    expect(pulled.length).toBeGreaterThan(0);
    for (const repo of new Set(pulled)) {
      expect(created).toContain(repo);
    }
  });

  test("the image is built on an arm64 host, because the nodes are Graviton", () => {
    // An amd64 image is worse than a missing one: the pod starts, then dies with
    // an exec format error that reads like an application crash.
    coreTemplate.hasResourceProperties("AWS::CodeBuild::Project", {
      Environment: Match.objectLike({
        Type: "ARM_CONTAINER",
        // Docker daemon inside the build container.
        PrivilegedMode: true,
      }),
    });
  });

  test("the build refuses a context carrying wallet seeds", () => {
    const projects = coreTemplate.findResources("AWS::CodeBuild::Project");
    const spec = JSON.stringify(Object.values(projects)[0]);

    expect(spec).toContain("config/wallets.json");
    expect(spec).toContain("refusing to build");
    // And it builds for the right platform explicitly, not just on an arm host.
    expect(spec).toContain("--platform linux/arm64");
  });

  test("the build source bucket is private and TLS-only", () => {
    coreTemplate.hasResourceProperties("AWS::S3::Bucket", {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });

    const policies = JSON.stringify(
      coreTemplate.findResources("AWS::S3::BucketPolicy")
    );
    expect(policies).toContain("aws:SecureTransport");
  });
});
