import type { Tool } from "@ag-ui/client";
import { FINANCIAL_COMPONENTS, type FinancialComponent } from "./financial-components";

export const AGENT_ID = "xrpl_transfer_assistant";

const QUOTE_FIELDS = [
  "corridor",
  "sourceAmount",
  "destinationAmount",
  "sendMax",
  "serviceFee",
  "route",
  "expiresAt",
] as const;

export type QuoteCardArgs = Record<(typeof QUOTE_FIELDS)[number], string>;

const stringProperties = (names: readonly string[]) =>
  Object.fromEntries(names.map((name) => [name, { type: "string" }]));

const transferIdSchema = {
  type: "object",
  properties: {
    transferId: {
      type: "string",
      description: "Opaque transfer identifier returned by create_transfer_intent",
    },
  },
  required: ["transferId"],
  additionalProperties: false,
};

// Client-declared AG-UI tools. The Runtime rejects any tool not in its own
// allowlist, and the UI renders only these names, as application-owned cards.
export const FINANCIAL_TOOLS: readonly Tool[] = Object.freeze(
  FINANCIAL_COMPONENTS.map((name): Tool =>
    name === "render_quote_card"
      ? {
          name,
          description: "Render an application-owned read-only quote card.",
          parameters: {
            type: "object",
            properties: stringProperties(QUOTE_FIELDS),
            required: [...QUOTE_FIELDS],
            additionalProperties: false,
          },
        }
      : {
          name,
          description:
            "Render an application-owned transfer component backed by the authoritative REST API.",
          parameters: transferIdSchema,
        },
  ),
);

export const TOOL_RESULTS: Record<FinancialComponent, string> = {
  render_quote_card: "Quote card rendered.",
  render_approval_card: "Authoritative transfer card rendered.",
  render_fee_progress: "Authoritative transfer card rendered.",
  render_settlement_progress: "Authoritative transfer card rendered.",
  render_receipt: "Authoritative transfer card rendered.",
  render_transfer_failure: "Authoritative transfer card rendered.",
};

// Arguments stream in as partial JSON; they only parse once the call is complete.
function parseArgs(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** Keep only the declared string fields; the model's extra keys never reach a card. */
export function quoteCardArgs(raw: string): QuoteCardArgs | null {
  const args = parseArgs(raw);
  if (!args) return null;
  return Object.fromEntries(
    QUOTE_FIELDS.map((field) => [field, text(args[field])]),
  ) as QuoteCardArgs;
}

export function transferIdArg(raw: string): string {
  return text(parseArgs(raw)?.transferId);
}
