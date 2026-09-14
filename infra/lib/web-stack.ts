/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as autoscaling from "aws-cdk-lib/aws-autoscaling";
import * as iam from "aws-cdk-lib/aws-iam";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53targets from "aws-cdk-lib/aws-route53-targets";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

/**
 * Custom-domain + TLS configuration for the web stack. REQUIRED.
 *
 * This used to be optional, and omitting it deployed a public HTTP-only
 * listener. That is not a "demo convenience": the site's first request is a
 * login form, so a plaintext listener publishes the password and the session
 * cookie to every hop on the path, and no `Secure` cookie flag can be honoured.
 * There is no HTTP-only code path any more — synthesis fails instead.
 */
export interface DomainConfig {
  /** Apex domain already hosted in Route 53, e.g. "example.com" */
  domainName: string;
  /** Subdomain to create, e.g. "xrpl-agentic-payments" -> xrpl-agentic-payments.example.com */
  subdomain: string;
  /** ARN of an existing ACM certificate covering the subdomain (or a wildcard) */
  certificateArn: string;
}

export interface XrplAgenticPaymentsWebStackProps extends cdk.StackProps {
  /** Custom domain + ACM certificate. Required — see DomainConfig. */
  domain: DomainConfig;
}

/**
 * XRPL Agentic Payments Web Frontend Stack
 *
 * ALB + EC2 Auto Scaling Group serving the conversational UI, HTTPS-only:
 * an HTTPS listener on the supplied ACM cert, a Route 53 alias record, and a
 * port-80 listener that does nothing but 301 to HTTPS.
 */
export class XrplAgenticPaymentsWebStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: XrplAgenticPaymentsWebStackProps) {
    super(scope, id, props);

    // Fail at synth rather than deploying a public plaintext login page. The
    // check is here as well as in bin/infra.ts because the stack is exported
    // and can be instantiated directly.
    const domain = props?.domain;
    if (!domain?.domainName || !domain?.certificateArn) {
      throw new Error(
        "XrplAgenticPaymentsWebStack requires TLS: pass domain.domainName and " +
          "domain.certificateArn (cdk deploy --context domainName=example.com " +
          "--context certificateArn=arn:aws:acm:...). Serving the login form " +
          "over plaintext HTTP is not a supported configuration."
      );
    }

    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;
    const ecrRegistry = `${account}.dkr.ecr.${region}.amazonaws.com`;

    // ─────────────────────────────────────────────────────────────────
    // 0. Web auth secret — never hardcode credentials in source.
    //    Generates a random password on first deploy if one isn't supplied.
    // ─────────────────────────────────────────────────────────────────
    const webAuthSecret = new secretsmanager.Secret(this, "WebAuthSecret", {
      secretName: "xrpl-agentic-payments/web-auth",
      description: "Username/password for the XRPL Agentic Payments web UI",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ username: "admin" }),
        generateStringKey: "password",
        excludePunctuation: true,
        passwordLength: 24,
      },
    });

    // ─────────────────────────────────────────────────────────────────
    // 1. VPC — 2 AZs, public subnets only (cost-optimized for prototype)
    // ─────────────────────────────────────────────────────────────────
    const vpc = new ec2.Vpc(this, "XrplAgenticPaymentsVPC", {
      maxAzs: 2,
      natGateways: 0, // No NAT — instances are in public subnets
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
        },
      ],
    });

    // ─────────────────────────────────────────────────────────────────
    // 2. Security Groups
    // ─────────────────────────────────────────────────────────────────
    const albSg = new ec2.SecurityGroup(this, "ALB-SG", {
      vpc,
      description: "XRPL Agentic Payments ALB - allows HTTP(S) from internet",
      allowAllOutbound: true,
    });
    // Port 80 is open only so the redirect listener below can answer it; no
    // application traffic is ever served on it.
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), "HTTP (redirect to HTTPS only)");
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS");

    const ec2Sg = new ec2.SecurityGroup(this, "EC2-SG", {
      vpc,
      description: "XRPL Agentic Payments EC2 - allows traffic from ALB only",
      allowAllOutbound: true,
    });
    ec2Sg.addIngressRule(albSg, ec2.Port.tcp(8080), "From ALB");

    // ─────────────────────────────────────────────────────────────────
    // 3. IAM Role for EC2 instances
    // ─────────────────────────────────────────────────────────────────
    const instanceRole = new iam.Role(this, "EC2Role", {
      roleName: "xrpl-agentic-payments-web-ec2",
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore"),
        iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonEC2ContainerRegistryReadOnly"),
      ],
    });

    // Permissions for the webapp to call Bedrock + AgentCore
    instanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${account}:inference-profile/*`,
        ],
      })
    );
    instanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/*`],
      })
    );
    instanceRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:GetMetricStatistics"],
        resources: ["*"],
        conditions: {
          StringEquals: { "aws:RequestedRegion": region },
        },
      })
    );
    webAuthSecret.grantRead(instanceRole);

    // ─────────────────────────────────────────────────────────────────
    // 4. Auto Scaling Group — Graviton t4g.small, Amazon Linux 2023
    // ─────────────────────────────────────────────────────────────────
    const asg = new autoscaling.AutoScalingGroup(this, "ASG", {
      vpc,
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T4G,
        ec2.InstanceSize.SMALL
      ),
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: ec2.AmazonLinuxCpuType.ARM_64,
      }),
      role: instanceRole,
      securityGroup: ec2Sg,
      minCapacity: 1,
      maxCapacity: 2,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      associatePublicIpAddress: true,
    });

    // UserData: install Docker, fetch web-auth secret, pull image from ECR, run container.
    // Credentials are read from Secrets Manager at boot time — nothing is
    // baked into the AMI, the container image, or this source file.
    const imageTag = this.node.tryGetContext("webImageTag") ?? "latest";
    asg.addUserData(
      "#!/bin/bash",
      "set -e",
      "",
      "# Install Docker",
      "dnf install -y docker jq",
      "systemctl enable docker && systemctl start docker",
      "",
      "# Fetch web auth credentials from Secrets Manager",
      `SECRET_JSON=$(aws secretsmanager get-secret-value --region ${region} --secret-id ${webAuthSecret.secretName} --query SecretString --output text)`,
      "XRPL_AGENTIC_USERNAME=$(echo \"$SECRET_JSON\" | jq -r .username)",
      "XRPL_AGENTIC_PASSWORD=$(echo \"$SECRET_JSON\" | jq -r .password)",
      "",
      "# Login to ECR",
      `aws ecr get-login-password --region ${region} | docker login --username AWS --password-stdin ${ecrRegistry}`,
      "",
      "# Pull and run the webapp container",
      `docker pull ${ecrRegistry}/xrpl-agentic-payments-webapp:${imageTag}`,
      "docker run -d --restart=always --name xrpl-agentic-payments \\",
      "  -p 8080:8080 \\",
      '  -e XRPL_AGENTIC_USERNAME="$XRPL_AGENTIC_USERNAME" \\',
      '  -e XRPL_AGENTIC_PASSWORD="$XRPL_AGENTIC_PASSWORD" \\',
      `  -e AWS_DEFAULT_REGION=${region} \\`,
      `  ${ecrRegistry}/xrpl-agentic-payments-webapp:${imageTag}`
    );

    // ─────────────────────────────────────────────────────────────────
    // 5. Application Load Balancer
    // ─────────────────────────────────────────────────────────────────
    const alb = new elbv2.ApplicationLoadBalancer(this, "ALB", {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, "TG", {
      vpc,
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [asg],
      healthCheck: {
        path: "/health",
        interval: cdk.Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    // ─── HTTPS via the supplied ACM cert (the only way traffic is served) ───
    const cert = acm.Certificate.fromCertificateArn(
      this,
      "WildcardCert",
      domain.certificateArn
    );

    alb.addListener("HTTPS", {
      port: 443,
      certificates: [cert],
      // Pinned rather than left to the ALB default, which still negotiates
      // TLS 1.0/1.1 on older policies.
      sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
      defaultAction: elbv2.ListenerAction.forward([targetGroup]),
    });

    // 301, not 302: the redirect is permanent so browsers and HSTS-aware
    // clients stop issuing the plaintext request at all.
    alb.addListener("HTTP", {
      port: 80,
      defaultAction: elbv2.ListenerAction.redirect({
        protocol: "HTTPS",
        port: "443",
        permanent: true,
      }),
    });

    // ─────────────────────────────────────────────────────────────
    // Route 53 — A record alias to ALB (requires an existing hosted zone)
    // ─────────────────────────────────────────────────────────────
    const hostedZone = route53.HostedZone.fromLookup(this, "Zone", {
      domainName: domain.domainName,
    });

    new route53.ARecord(this, "AliasRecord", {
      zone: hostedZone,
      recordName: domain.subdomain,
      target: route53.RecordTarget.fromAlias(
        new route53targets.LoadBalancerTarget(alb)
      ),
    });

    const websiteUrl = `https://${domain.subdomain}.${domain.domainName}`;

    // ─────────────────────────────────────────────────────────────────
    // 6. Outputs
    // ─────────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "ALBDnsName", {
      value: alb.loadBalancerDnsName,
      description: "ALB DNS name",
    });

    new cdk.CfnOutput(this, "WebsiteUrl", {
      value: websiteUrl,
      description: "XRPL Agentic Payments website URL",
    });

    new cdk.CfnOutput(this, "WebAuthSecretName", {
      value: webAuthSecret.secretName,
      description:
        "Secrets Manager secret holding the web UI username/password. Retrieve with: aws secretsmanager get-secret-value --secret-id " +
        webAuthSecret.secretName,
    });

    // Tags
    cdk.Tags.of(this).add("Project", "XrplAgenticPayments");
    cdk.Tags.of(this).add("Environment", "development");
    cdk.Tags.of(this).add("ManagedBy", "cdk");
    cdk.Tags.of(this).add("Phase", "prototype");
  }
}
