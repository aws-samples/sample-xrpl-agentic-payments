#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { XrplAgentCoreStack } from "../lib/xrpl-agentcore-stack";

const app = new cdk.App();

new XrplAgentCoreStack(app, "XrplAgentCorePoc", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-west-2",
  },
  runtimeImageTag: app.node.tryGetContext("runtimeImageTag") ?? "latest",
});
