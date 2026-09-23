export const FINANCIAL_COMPONENTS = Object.freeze([
  "render_quote_card",
  "render_approval_card",
  "render_fee_progress",
  "render_settlement_progress",
  "render_receipt",
  "render_transfer_failure",
] as const);

export type FinancialComponent = (typeof FINANCIAL_COMPONENTS)[number];

export function isFinancialComponent(value: string): value is FinancialComponent {
  return (FINANCIAL_COMPONENTS as readonly string[]).includes(value);
}
