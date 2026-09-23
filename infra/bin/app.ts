#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { XrplAgentCoreStack } from "../lib/xrpl-agentcore-stack";

const app = new cdk.App();

new XrplAgentCoreStack(app, "XrplAgentCorePoc", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    // Read from CDK context (-c region=...), not CDK_DEFAULT_REGION: the cdk
    // CLI sets that env var itself from the caller's default profile/config
    // region and overwrites any value a wrapper script exports before
    // invoking it, so a profile configured for a different region would
    // silently redirect this deploy there. scripts/deploy.sh passes the
    // deploying user's chosen region — read from .env, its one source of
    // truth — as context explicitly, which the CLI never touches. Falls back
    // to the CLI's own resolution for a direct `cdk deploy`/`cdk synth` with
    // no context set; no hardcoded region default lives here.
    region: app.node.tryGetContext("region") ?? process.env.CDK_DEFAULT_REGION,
  },
  runtimeImageTag: app.node.tryGetContext("runtimeImageTag") ?? "latest",
});
