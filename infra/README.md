# XRPL Agentic Payments — CDK infrastructure

This is the AWS CDK (TypeScript) app for XRPL Agentic Payments. It deploys
`XrplAgenticPaymentsStack` (core IAM/ECR/secrets), `XrplAgenticPaymentsWebStack`
(web frontend), `XrplAgenticPaymentsEksV2` (EKS), and
`XrplAgenticPaymentsToolsStack` (Lambda tools).

The `cdk.json` file tells the CDK Toolkit how to execute your app.

## Useful commands

* `npm run build`   compile typescript to js
* `npm run watch`   watch for changes and compile
* `npm run test`    perform the jest unit tests
* `npx cdk deploy`  deploy this stack to your default AWS account/region
* `npx cdk diff`    compare deployed stack with current state
* `npx cdk synth`   emits the synthesized CloudFormation template
