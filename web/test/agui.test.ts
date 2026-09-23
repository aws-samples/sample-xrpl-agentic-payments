import { describe, expect, it } from "vitest";
import { FINANCIAL_TOOLS, quoteCardArgs, transferIdArg } from "@/lib/agui";
import { FINANCIAL_COMPONENTS } from "@/lib/financial-components";

describe("AG-UI client tools", () => {
  it("declares exactly the allowlisted render tools", () => {
    expect(FINANCIAL_TOOLS.map((tool) => tool.name)).toEqual([
      ...FINANCIAL_COMPONENTS,
    ]);
  });

  it("does not render a card from partial streamed arguments", () => {
    expect(quoteCardArgs('{"corridor": "usd-mx')).toBeNull();
    expect(transferIdArg('{"transferId": "tr_')).toBe("");
  });

  it("keeps only declared quote fields", () => {
    const args = quoteCardArgs(
      JSON.stringify({ corridor: "usd-mxn-testnet", html: "<script>", sendMax: 5 }),
    );
    expect(args).not.toBeNull();
    expect(Object.keys(args!)).not.toContain("html");
    expect(args!.corridor).toBe("usd-mxn-testnet");
    expect(args!.sendMax).toBe("");
  });
});
