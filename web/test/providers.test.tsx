// @vitest-environment jsdom

import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ accessToken: null as string | null }));

vi.mock("@/components/auth-context", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({ accessToken: auth.accessToken }),
}));

vi.mock("@/components/app-frame", () => ({
  AppFrame: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-frame">{children}</div>
  ),
}));

vi.mock("@/components/agent-session", () => ({
  AgentSessionProvider: ({
    children,
    accessToken,
    forwardedProps,
  }: {
    children: React.ReactNode;
    accessToken: string;
    forwardedProps: { memoryOptIn: boolean; preferences: object };
  }) => (
    <div
      data-testid="agent-session"
      data-has-authorization={String(Boolean(accessToken))}
      data-memory-opt-in={String(forwardedProps.memoryOptIn)}
      data-preference-count={String(Object.keys(forwardedProps.preferences).length)}
    >
      {children}
    </div>
  ),
}));

import { AuthenticatedAgent } from "../app/providers";

afterEach(() => {
  cleanup();
  auth.accessToken = null;
  window.localStorage.clear();
});

describe("authenticated AG-UI session lifecycle", () => {
  it("does not contact the runtime before Cognito authentication", () => {
    render(
      <AuthenticatedAgent>
        <span>page</span>
      </AuthenticatedAgent>,
    );

    expect(screen.getByTestId("app-frame")).toBeTruthy();
    expect(screen.queryByTestId("agent-session")).toBeNull();
  });

  it("mounts the AG-UI session with the Cognito bearer token after sign-in", () => {
    auth.accessToken = "test-access-token";

    render(
      <AuthenticatedAgent>
        <span>page</span>
      </AuthenticatedAgent>,
    );

    expect(screen.getByTestId("agent-session").dataset.hasAuthorization).toBe(
      "true",
    );
  });

  it("forwards preferences only when memory is opted in", () => {
    auth.accessToken = "test-access-token";
    window.localStorage.setItem(
      "xrpl-transfer-preferences",
      JSON.stringify({ memory_opt_in: false, default_slippage_bps: 50 }),
    );

    render(
      <AuthenticatedAgent>
        <span>page</span>
      </AuthenticatedAgent>,
    );

    const session = screen.getByTestId("agent-session");
    expect(session.dataset.memoryOptIn).toBe("false");
    expect(session.dataset.preferenceCount).toBe("0");
  });
});
