import { describe, expect, it } from "vitest";
import {
  FINANCIAL_COMPONENTS,
  isFinancialComponent,
} from "../lib/financial-components";

describe("AG-UI financial component allowlist", () => {
  it("contains only application-owned rendering actions", () => {
    expect(FINANCIAL_COMPONENTS).toEqual([
      "render_quote_card",
      "render_approval_card",
      "render_fee_progress",
      "render_settlement_progress",
      "render_receipt",
      "render_transfer_failure",
    ]);
    expect(isFinancialComponent("render_receipt")).toBe(true);
  });

  it.each([
    "approve_transfer",
    "execute_transfer",
    "render_model_html",
    "start_workflow",
  ])("does not authorize %s", (name) => {
    expect(isFinancialComponent(name)).toBe(false);
    expect(FINANCIAL_COMPONENTS).not.toContain(name);
  });
});
