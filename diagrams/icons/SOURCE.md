# Icon provenance

All icons are official vendor marks, so they are **not committed** — this file
is, and it is enough to rebuild the directory. `.gitignore` keeps
`diagrams/icons/*.png` out of the repo rather than redistributing AWS's marks.

To render on a fresh clone, download the packages below and rasterise or copy
the listed files into this directory under the listed names. Until then
`render_architecture.py` fails its own `check_art` and names the missing file
before drawing anything, so a half-iconned diagram never gets written.

## AWS service and resource icons

- **Source:** AWS Architecture Icons package (`Icon-package_04302026`), from
  https://aws.amazon.com/architecture/icons/ — SVG rasterised with
  `rsvg-convert -h 384 -a <svg> -o diagrams/icons/<name>.png`.

| File | Package path |
|------|--------------|
| `user.png` | `Resource-Icons_*/Res_General-Icons/Res_48_Light/Res_User_48_Light.svg` |
| `client.png` | `Resource-Icons_*/Res_General-Icons/Res_48_Light/Res_Client_48_Light.svg` |
| `cognito.png` | `Arch_Security-Identity/64/Arch_Amazon-Cognito_64.svg` |
| `apigw.png` | `Arch_Networking-Content-Delivery/64/Arch_Amazon-API-Gateway_64.svg` |
| `lambda.png` | `Arch_Compute/64/Arch_AWS-Lambda_64.svg` |
| `dynamodb.png` | `Arch_Databases/64/Arch_Amazon-DynamoDB_64.svg` |
| `stepfunctions.png` | `Arch_Application-Integration/64/Arch_AWS-Step-Functions_64.svg` |
| `kms.png` | `Arch_Security-Identity/64/Arch_AWS-Key-Management-Service_64.svg` |
| `secrets.png` | `Arch_Security-Identity/64/Arch_AWS-Secrets-Manager_64.svg` |
| `ecr.png` | `Arch_Containers/64/Arch_Amazon-Elastic-Container-Registry_64.svg` |
| `bedrock.png` | `Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64.svg` |

The Next.js app uses the generic **Client** resource icon: it is not an AWS
service, and it runs on the operator's machine rather than in the account.

## Amazon Bedrock AgentCore sub-service icons

- **Source:** `Agentcore-Bedrock-Icons.pptx` (official AWS AgentCore icon deck).
  Extracted with `unzip -o -q Agentcore-Bedrock-Icons.pptx 'ppt/media/*'
  'ppt/slides/*' -d /tmp/acicons`; labels recovered by caption-geometry match
  (caption box directly beneath the picture) against slide 3.

| File | Media file | Deck caption |
|------|-----------|--------------|
| `runtime.png` | `image36.png` | "Runtime" |
| `gateway.png` | `image42.png` | "Gateway" |
| `memory.png` | `image28.png` | "Memory" |
| `policy.png` | `image40.png` | "Policy Engine / Agentic Guardrails" |

## Not available — drawn as glyph

- **XRP Ledger** has no AWS icon, and the XRP brand mark is a third-party
  trademark, so it is drawn as a glyph tile (`◈`) rather than an approximate
  substitute.
