"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useAuth } from "./auth-context";

function Login() {
  const { login, error } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await login(email, password);
    } catch {
      // AuthProvider owns the user-facing error and clears it on the next try.
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-shell">
      <section className="login-card">
        <p className="eyebrow">XRPL Testnet</p>
        <h1>Cross-border transfers, with an agent you can audit.</h1>
        <p className="muted">
          Sign in through the POC Cognito user pool. No Mainnet funds or real
          fiat move in this demo.
        </p>
        <form onSubmit={submit}>
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {error && <p className="error-text">{error}</p>}
          <button className="primary-button" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}

export function AppFrame({ children }: { children: React.ReactNode }) {
  const { accessToken, loading, logout } = useAuth();
  if (loading) return <main className="loading-shell">Loading secure session…</main>;
  if (!accessToken) return <Login />;

  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="brand" href="/">
          <span className="brand-mark">X</span>
          AgentSwift
        </Link>
        <nav>
          <Link href="/">Transfer</Link>
          <Link href="/history">History</Link>
          <Link href="/settings">Settings</Link>
          <Link href="/fixtures">Fixtures</Link>
        </nav>
        <button className="quiet-button" onClick={() => void logout()}>
          Sign out
        </button>
      </header>
      <div className="demo-banner">
        Demo fixtures · XRPL Testnet · simulated sanctions and fiat payout
      </div>
      {children}
    </div>
  );
}
