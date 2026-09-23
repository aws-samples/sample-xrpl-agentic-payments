"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiRequest, getTransfer } from "@/lib/api";
import type { Amount, TransferProjection, TransferStatus } from "@/lib/types";
import { useAuth } from "./auth-context";

const terminal = new Set<TransferStatus>([
  "COMPLETED",
  "EXPIRED",
  "FAILED",
  "FAILED_REFUNDED",
]);

const statusOrder: TransferStatus[] = [
  "AWAITING_APPROVAL",
  "APPROVED",
  "FEE_PENDING",
  "FEE_PAID",
  "PREPARING",
  "SUBMISSION_UNKNOWN",
  "SUBMITTED",
  "SETTLED",
  "PAYOUT_PENDING",
  "COMPLETED",
];

function amountLabel(amount: Amount): string {
  return `${amount.value} ${amount.asset.currency}`;
}

function statusLabel(status: TransferStatus): string {
  return status.toLowerCase().replaceAll("_", " ");
}

function issuerLabel(amount: Amount): string {
  const issuer = amount.asset.issuer;
  if (!issuer) return "XRPL native asset";
  return `${issuer.slice(0, 8)}…${issuer.slice(-6)}`;
}

export function useTransfer(transferId: string) {
  const { accessToken } = useAuth();
  const [transfer, setTransfer] = useState<TransferProjection | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!accessToken || !transferId) return;
    try {
      setTransfer(await getTransfer(accessToken, transferId));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Status unavailable");
    }
  }, [accessToken, transferId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!transfer || terminal.has(transfer.status)) return;
    const interval = window.setInterval(() => void refresh(), 3_000);
    return () => window.clearInterval(interval);
  }, [refresh, transfer]);

  return { transfer, error, refresh };
}

export function TransferCard({ transferId }: { transferId: string }) {
  const { accessToken } = useAuth();
  const { transfer, error, refresh } = useTransfer(transferId);
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  const currentIndex = useMemo(
    () => (transfer ? statusOrder.indexOf(transfer.status) : -1),
    [transfer],
  );

  const approve = async () => {
    if (!accessToken || !transfer) return;
    setApproving(true);
    setApprovalError(null);
    const storageKey = `xrpl-approval-${transfer.transfer_id}`;
    let idempotencyKey = window.localStorage.getItem(storageKey);
    if (!idempotencyKey) {
      idempotencyKey = `browser-${crypto.randomUUID()}`;
      window.localStorage.setItem(storageKey, idempotencyKey);
    }
    try {
      await apiRequest<TransferProjection>(
        accessToken,
        `/v1/transfers/${encodeURIComponent(transfer.transfer_id)}/approve`,
        {
          method: "POST",
          body: JSON.stringify({
            approval_hash: transfer.approval_hash,
            idempotency_key: idempotencyKey,
          }),
        },
      );
      await refresh();
    } catch (caught) {
      setApprovalError(
        caught instanceof Error ? caught.message : "Approval failed",
      );
    } finally {
      setApproving(false);
    }
  };

  if (error) {
    return <section className="financial-card error-card">{error}</section>;
  }
  if (!transfer) {
    return <section className="financial-card">Loading authoritative status…</section>;
  }

  const failed = ["FAILED", "FAILED_REFUNDED", "EXPIRED"].includes(
    transfer.status,
  );
  return (
    <section className={`financial-card ${failed ? "error-card" : ""}`}>
      <div className="card-heading">
        <div>
          <p className="eyebrow">Transfer {transfer.transfer_id.slice(-8)}</p>
          <h3>{amountLabel(transfer.destination_amount)}</h3>
          <p className="muted">to {transfer.recipient_display}</p>
        </div>
        <span className={`status-pill status-${transfer.status.toLowerCase()}`}>
          {statusLabel(transfer.status)}
        </span>
      </div>

      <dl className="quote-grid">
        <div>
          <dt>Source estimate</dt>
          <dd>{amountLabel(transfer.source_amount)}</dd>
        </div>
        <div>
          <dt>Maximum send</dt>
          <dd>{amountLabel(transfer.send_max)}</dd>
        </div>
        <div>
          <dt>Slippage ceiling</dt>
          <dd>{transfer.slippage_bps} bps</dd>
        </div>
        <div>
          <dt>x402 service fee</dt>
          <dd>{amountLabel(transfer.service_fee.amount)}</dd>
        </div>
        <div>
          <dt>Payout</dt>
          <dd>
            {transfer.payout_mode === "XRPL_WALLET"
              ? "Direct XRPL wallet"
              : "Simulated local fiat"}
          </dd>
        </div>
        <div>
          <dt>Source issuer</dt>
          <dd title={transfer.source_amount.asset.issuer ?? "XRPL native"}>
            {issuerLabel(transfer.source_amount)}
          </dd>
        </div>
        <div>
          <dt>Destination issuer</dt>
          <dd title={transfer.destination_amount.asset.issuer ?? "XRPL native"}>
            {issuerLabel(transfer.destination_amount)}
          </dd>
        </div>
      </dl>
      <p className="route-copy">{transfer.route_label}</p>

      {transfer.status === "AWAITING_APPROVAL" && (
        <div className="approval-box">
          <strong>Explicit approval required</strong>
          <p>
            This confirms the route, SendMax, recipient, payout mode, expiry,
            and exact XRP fee shown here.
          </p>
          <dl className="approval-contract">
            <div>
              <dt>
                {transfer.payout_mode === "XRPL_WALLET"
                  ? "Recipient wallet"
                  : "Simulation settlement wallet"}
              </dt>
              <dd>
                <code>{transfer.recipient_destination}</code>
              </dd>
            </div>
            <div>
              <dt>Destination tag</dt>
              <dd>{transfer.destination_tag ?? "None"}</dd>
            </div>
            <div>
              <dt>Source asset issuer</dt>
              <dd>
                <code>
                  {transfer.source_amount.asset.issuer ?? "XRPL native"}
                </code>
              </dd>
            </div>
            <div>
              <dt>Destination asset issuer</dt>
              <dd>
                <code>
                  {transfer.destination_amount.asset.issuer ?? "XRPL native"}
                </code>
              </dd>
            </div>
            <div>
              <dt>x402 fee recipient</dt>
              <dd>
                <code>{transfer.service_fee.pay_to}</code>
              </dd>
            </div>
            <div>
              <dt>x402 protocol</dt>
              <dd>
                {transfer.service_fee.scheme} · {transfer.service_fee.network} ·
                SourceTag {transfer.service_fee.source_tag}
              </dd>
            </div>
            <div>
              <dt>Quote expires</dt>
              <dd>{new Date(transfer.expires_at).toLocaleString()}</dd>
            </div>
            <div>
              <dt>Approval commitment</dt>
              <dd>
                <code title={transfer.approval_hash}>
                  {transfer.approval_hash.slice(0, 16)}…
                </code>
              </dd>
            </div>
          </dl>
          <button
            className="primary-button"
            disabled={approving}
            onClick={() => void approve()}
          >
            {approving ? "Recording approval…" : "Approve and execute"}
          </button>
          {approvalError && <p className="error-text">{approvalError}</p>}
        </div>
      )}

      {!failed && transfer.status !== "AWAITING_APPROVAL" && (
        <ol className="status-track" aria-label="Transfer progress">
          {["APPROVED", "FEE_PAID", "SUBMITTED", "SETTLED", "COMPLETED"].map(
            (status) => {
              const index = statusOrder.indexOf(status as TransferStatus);
              return (
                <li className={currentIndex >= index ? "complete" : ""} key={status}>
                  {statusLabel(status as TransferStatus)}
                </li>
              );
            },
          )}
        </ol>
      )}

      {transfer.fee_tx_hash && (
        <p className="hash-row">
          x402 fee{" "}
          <a
            href={`https://testnet.xrpl.org/transactions/${transfer.fee_tx_hash}`}
            target="_blank"
            rel="noreferrer"
          >
            {transfer.fee_tx_hash.slice(0, 12)}…
          </a>
        </p>
      )}
      {transfer.payment_tx_hash && (
        <p className="hash-row">
          Transfer{" "}
          <a
            href={
              transfer.explorer_url ??
              `https://testnet.xrpl.org/transactions/${transfer.payment_tx_hash}`
            }
            target="_blank"
            rel="noreferrer"
          >
            {transfer.payment_tx_hash.slice(0, 12)}…
          </a>
        </p>
      )}
      {transfer.payout_reference && (
        <p className="hash-row">
          Simulated payout <code>{transfer.payout_reference}</code>
        </p>
      )}
      {failed && (
        <div className="failure-detail">
          <strong>{transfer.failure_code ?? transfer.status}</strong>
          <p>{transfer.failure_reason ?? "This transfer did not complete."}</p>
        </div>
      )}
    </section>
  );
}

export interface QuoteCardProps {
  corridor: string;
  sourceAmount: string;
  destinationAmount: string;
  sendMax: string;
  serviceFee: string;
  route: string;
  expiresAt: string;
}

export function QuoteCard(props: QuoteCardProps) {
  return (
    <section className="financial-card">
      <div className="card-heading">
        <div>
          <p className="eyebrow">Structured Testnet quote</p>
          <h3>{props.destinationAmount}</h3>
        </div>
        <span className="status-pill">demo fixture</span>
      </div>
      <dl className="quote-grid">
        <div>
          <dt>Corridor</dt>
          <dd>{props.corridor}</dd>
        </div>
        <div>
          <dt>Source estimate</dt>
          <dd>{props.sourceAmount}</dd>
        </div>
        <div>
          <dt>SendMax</dt>
          <dd>{props.sendMax}</dd>
        </div>
        <div>
          <dt>x402 fee</dt>
          <dd>{props.serviceFee}</dd>
        </div>
      </dl>
      <p className="route-copy">{props.route}</p>
      <p className="muted">Expires {new Date(props.expiresAt).toLocaleTimeString()}</p>
    </section>
  );
}
