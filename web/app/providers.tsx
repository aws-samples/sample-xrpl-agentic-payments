"use client";

import { useEffect, useMemo, useState } from "react";
import { AgentSessionProvider } from "@/components/agent-session";
import { AuthProvider, useAuth } from "@/components/auth-context";
import { AppFrame } from "@/components/app-frame";
import type { PreferenceSettings } from "@/lib/types";

const defaultPreferences: PreferenceSettings = {
  memory_opt_in: false,
  default_corridor_id: "usd-mxn-testnet",
  default_payout_mode: "XRPL_WALLET",
  default_slippage_bps: 100,
};

export function AuthenticatedAgent({
  children,
}: {
  children: React.ReactNode;
}) {
  const { accessToken } = useAuth();
  const [preferences, setPreferences] =
    useState<PreferenceSettings>(defaultPreferences);

  useEffect(() => {
    const load = () => {
      const raw = window.localStorage.getItem("xrpl-transfer-preferences");
      if (!raw) return setPreferences(defaultPreferences);
      try {
        setPreferences({ ...defaultPreferences, ...JSON.parse(raw) });
      } catch {
        setPreferences(defaultPreferences);
      }
    };
    load();
    window.addEventListener("xrpl-preferences-updated", load);
    return () => window.removeEventListener("xrpl-preferences-updated", load);
  }, []);

  const forwardedProps = useMemo(
    () => ({
      memoryOptIn: preferences.memory_opt_in,
      preferences: preferences.memory_opt_in
        ? {
            default_corridor_id: preferences.default_corridor_id,
            default_payout_mode: preferences.default_payout_mode,
            default_slippage_bps: preferences.default_slippage_bps,
          }
        : {},
    }),
    [preferences],
  );

  // Mount the AG-UI session only after Cognito has supplied a token, so no
  // agent run is attempted unauthenticated and each sign-in starts a fresh
  // thread.
  if (!accessToken) {
    return <AppFrame>{children}</AppFrame>;
  }

  return (
    <AgentSessionProvider
      accessToken={accessToken}
      forwardedProps={forwardedProps}
    >
      <AppFrame>{children}</AppFrame>
    </AgentSessionProvider>
  );
}

export function AppProviders({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AuthenticatedAgent>{children}</AuthenticatedAgent>
    </AuthProvider>
  );
}
