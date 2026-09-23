"use client";

import { createContext, useContext, useMemo } from "react";

export interface AgentForwardedProps {
  memoryOptIn: boolean;
  preferences: Record<string, unknown>;
}

interface AgentSession {
  runtimeUrl: string;
  headers: Record<string, string>;
  forwardedProps: AgentForwardedProps;
}

const AgentSessionContext = createContext<AgentSession | null>(null);

export function AgentSessionProvider({
  accessToken,
  forwardedProps,
  children,
}: {
  accessToken: string;
  forwardedProps: AgentForwardedProps;
  children: React.ReactNode;
}) {
  const value = useMemo(
    () => ({
      runtimeUrl: "/api/agui",
      headers: { Authorization: `Bearer ${accessToken}` },
      forwardedProps,
    }),
    [accessToken, forwardedProps],
  );
  return (
    <AgentSessionContext.Provider value={value}>
      {children}
    </AgentSessionContext.Provider>
  );
}

export function useAgentSession(): AgentSession {
  const value = useContext(AgentSessionContext);
  if (!value) {
    throw new Error("useAgentSession must be used inside AgentSessionProvider");
  }
  return value;
}
