import type { TransferProjection } from "./types";

const apiUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

export async function apiRequest<T>(
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  if (!apiUrl) throw new Error("NEXT_PUBLIC_API_BASE_URL is not configured");
  const response = await fetch(`${apiUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `API request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function getTransfer(
  token: string,
  transferId: string,
): Promise<TransferProjection> {
  return apiRequest(token, `/v1/transfers/${encodeURIComponent(transferId)}`);
}
