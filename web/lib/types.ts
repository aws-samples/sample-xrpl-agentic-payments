export type PayoutMode = "XRPL_WALLET" | "LOCAL_FIAT_SIMULATED";

export interface Asset {
  currency: string;
  issuer?: string | null;
}

export interface Amount {
  asset: Asset;
  value: string;
}

export interface ServiceFee {
  network: "xrpl:1";
  scheme: "exact";
  amount: Amount;
  pay_to: string;
  source_tag: number;
  max_timeout_seconds: number;
}

export type TransferStatus =
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "FEE_PENDING"
  | "FEE_PAID"
  | "PREPARING"
  | "SUBMISSION_UNKNOWN"
  | "SUBMITTED"
  | "SETTLED"
  | "PAYOUT_PENDING"
  | "COMPLETED"
  | "EXPIRED"
  | "FAILED"
  | "REFUND_PENDING"
  | "FAILED_REFUNDED";

export interface TransferProjection {
  transfer_id: string;
  status: TransferStatus;
  payout_mode: PayoutMode;
  recipient_display: string;
  recipient_destination: string;
  destination_tag?: number | null;
  destination_amount: Amount;
  source_amount: Amount;
  send_max: Amount;
  slippage_bps: number;
  service_fee: ServiceFee;
  route_label: string;
  expires_at: string;
  approval_hash: string;
  fee_tx_hash?: string | null;
  payment_tx_hash?: string | null;
  ledger_index?: number | null;
  explorer_url?: string | null;
  payout_reference?: string | null;
  failure_code?: string | null;
  failure_reason?: string | null;
  revision: number;
}

export interface PreferenceSettings {
  memory_opt_in: boolean;
  default_corridor_id: string | null;
  default_payout_mode: PayoutMode | null;
  default_slippage_bps: number | null;
}
