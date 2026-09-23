#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { XrplAgentCoreStack } from "../lib/xrpl-agentcore-stack";

const app = new cdk.App();

new XrplAgentCoreStack(app, "XrplAgentCorePoc", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    // Hardcoded, not read from CDK_DEFAULT_REGION: the cdk CLI sets that env
    // var itself, from the caller's default profile/config region, and
    // overwrites any value a wrapper script exports before invoking it. A
    // profile whose config has a region other than us-west-2 would silently
    // redirect this deploy there. This stack, and scripts/deploy.sh, support
    // only us-west-2, so pin it rather than trust an env var the CLI owns.
    region: "us-west-2",
  },
  runtimeImageTag: app.node.tryGetContext("runtimeImageTag") ?? "latest",
});
