"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/components/auth-context";
import { TransferCard } from "@/components/transfer-card";
import { apiRequest } from "@/lib/api";
import type { TransferProjection } from "@/lib/types";

export default function HistoryPage() {
  const { accessToken } = useAuth();
  const [transfers, setTransfers] = useState<TransferProjection[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!accessToken) {
      setLoading(false);
      return;
    }
    apiRequest<TransferProjection[]>(accessToken, "/v1/transfers")
      .then(setTransfers)
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : "History unavailable"),
      )
      .finally(() => setLoading(false));
  }, [accessToken]);

  return (
    <main className="content-page">
      <p className="eyebrow">Durable records</p>
      <h1>Transfer history</h1>
      <p className="muted">
        These cards poll authoritative workflow state and recover after a
        browser refresh or completed agent session.
      </p>
      {error && <p className="error-text">{error}</p>}
      <div className="card-list">
        {loading && <p className="empty-state">Loading durable transfers…</p>}
        {transfers.map((transfer) => (
          <TransferCard key={transfer.transfer_id} transferId={transfer.transfer_id} />
        ))}
        {!loading && !error && transfers.length === 0 && (
          <p className="empty-state">No transfer intents yet.</p>
        )}
      </div>
    </main>
  );
}
