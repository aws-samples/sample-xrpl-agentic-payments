"use client";

import { FormEvent, useEffect, useState } from "react";
import { useAuth } from "@/components/auth-context";
import { apiRequest } from "@/lib/api";
import type { PreferenceSettings } from "@/lib/types";

const defaults: PreferenceSettings = {
  memory_opt_in: false,
  default_corridor_id: "usd-mxn-testnet",
  default_payout_mode: "XRPL_WALLET",
  default_slippage_bps: 100,
};

export default function SettingsPage() {
  const { accessToken } = useAuth();
  const [settings, setSettings] = useState(defaults);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const raw = window.localStorage.getItem("xrpl-transfer-preferences");
    if (raw) {
      try {
        setSettings({ ...defaults, ...JSON.parse(raw) });
      } catch {
        setSettings(defaults);
      }
    }
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setMessage(null);
    setError(null);
    window.localStorage.setItem(
      "xrpl-transfer-preferences",
      JSON.stringify(settings),
    );
    window.dispatchEvent(new Event("xrpl-preferences-updated"));
    try {
      if (accessToken && settings.memory_opt_in) {
        await apiRequest(accessToken, "/v1/preferences", {
          method: "POST",
          headers: {
            "X-Memory-Session": `settings-${crypto.randomUUID()}`,
          },
          body: JSON.stringify(settings),
        });
      }
      setMessage(
        settings.memory_opt_in
          ? "Preferences saved and submitted to opt-in AgentCore Memory."
          : "Preferences saved locally. AgentCore Memory recall is disabled.",
      );
    } catch (caught) {
      setError(
        `Preferences saved locally, but AgentCore Memory was unavailable: ${
          caught instanceof Error ? caught.message : "request failed"
        }`,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="content-page narrow-page">
      <p className="eyebrow">Privacy controls</p>
      <h1>Transfer preferences</h1>
      <p className="muted">
        Memory is off by default. It stores only the structured preferences
        below—never recipients, wallet addresses, amounts, bank details, or
        conversation transcripts.
      </p>
      <form className="settings-form" onSubmit={submit}>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={settings.memory_opt_in}
            onChange={(event) =>
              setSettings({ ...settings, memory_opt_in: event.target.checked })
            }
          />
          Opt in to AgentCore preference memory
        </label>
        <label>
          Default corridor
          <select
            value={settings.default_corridor_id ?? ""}
            onChange={(event) =>
              setSettings({
                ...settings,
                default_corridor_id: event.target.value,
              })
            }
          >
            <option value="usd-mxn-testnet">USD → MXN Testnet demo</option>
          </select>
        </label>
        <label>
          Default payout
          <select
            value={settings.default_payout_mode ?? "XRPL_WALLET"}
            onChange={(event) =>
              setSettings({
                ...settings,
                default_payout_mode: event.target
                  .value as PreferenceSettings["default_payout_mode"],
              })
            }
          >
            <option value="XRPL_WALLET">Direct XRPL wallet</option>
            <option value="LOCAL_FIAT_SIMULATED">Simulated local fiat</option>
          </select>
        </label>
        <label>
          Default slippage (basis points)
          <input
            type="number"
            min={0}
            max={1000}
            value={settings.default_slippage_bps ?? 100}
            onChange={(event) =>
              setSettings({
                ...settings,
                default_slippage_bps: Number(event.target.value),
              })
            }
          />
        </label>
        <button className="primary-button" disabled={saving}>
          {saving ? "Saving…" : "Save preferences"}
        </button>
        {message && <p className="success-text">{message}</p>}
        {error && <p className="error-text">{error}</p>}
      </form>
    </main>
  );
}
