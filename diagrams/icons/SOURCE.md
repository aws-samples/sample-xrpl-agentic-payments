# Icon provenance

All icons are official vendor marks, so they are **not committed** — this file
is, and it is enough to rebuild the directory. `.gitignore` keeps
`diagrams/icons/*.png` out of the repo rather than redistributing someone
else's marks.

To make the renderers work on a fresh clone, download the packages below and
rasterise the listed paths into this directory under the listed filenames:

    rsvg-convert -h 384 -a <svg> -o diagrams/icons/<name>.png

Until then every `render_*.py` fails its own `check_art` and names the missing
file before drawing anything, so a half-iconned diagram never gets written.

## AWS service icons
- **Source:** AWS Architecture Icons package (`Icon-package_04302026`), from
  https://aws.amazon.com/architecture/icons/ — SVG rasterised with
  `rsvg-convert -h 384 -a <svg> -o <png>`.

| File | Package path |
|------|--------------|
| `alb.png` | `Arch_Networking-Content-Delivery/64/Arch_Elastic-Load-Balancing_64.svg` |
| `eks.png` | `Arch_Containers/64/Arch_Amazon-Elastic-Kubernetes-Service_64.svg` |
| `lambda.png` | `Arch_Compute/64/Arch_AWS-Lambda_64.svg` |
| `bedrock.png` | `Arch_Artificial-Intelligence/64/Arch_Amazon-Bedrock_64.svg` |
| `cognito.png` | `Arch_Security-Identity/64/Arch_Amazon-Cognito_64.svg` |
| `secrets.png` | `Arch_Security-Identity/64/Arch_AWS-Secrets-Manager_64.svg` |
| `kms.png` | `Arch_Security-Identity/64/Arch_AWS-Key-Management-Service_64.svg` |
| `cloudwatch.png` | `Arch_Management-Tools/64/Arch_Amazon-CloudWatch_64.svg` |
| `cloudtrail.png` | `Arch_Management-Tools/64/Arch_AWS-CloudTrail_64.svg` |
| `user.png` | `Res_General-Icons/Res_48_Light/Res_User_48_Light.svg` |

## Amazon Bedrock AgentCore sub-service icons
- **Source:** `Agentcore-Bedrock-Icons.pptx` (official AWS AgentCore icon deck).
  Extracted from `ppt/media/`, labels recovered by caption-geometry match
  against slide 3.

| File | Media file | Deck caption |
|------|-----------|--------------|
| `harness.png` | `image24.png` | "AI Agent" (used for the AgentCore Harness / payment agent) |
| `gateway.png` | `image42.png` | "Gateway" |
| `runtime.png` | `image36.png` | "Runtime" (available; not wired into current diagram) |
| `agentcore.png` | `image26.png` | "Agentcore" (umbrella; not wired in) |

## Not available — drawn as glyph
- **XRP Ledger** has no AWS icon, and the XRP brand mark is a third-party
  trademark, so it is drawn as a lettered/glyph tile (`◈`) rather than an
  approximate substitute.
