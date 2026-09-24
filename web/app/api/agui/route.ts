import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 256 * 1024;

// Server-side AG-UI proxy. The upstream URL comes only from server config, so
// a caller cannot redirect the request; the caller's Cognito bearer token is
// forwarded for the Runtime's own JWT authorizer to validate.
export async function POST(request: NextRequest): Promise<Response> {
  const authorization = request.headers.get("authorization");
  if (!authorization?.startsWith("Bearer ")) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }
  const runtimeUrl = process.env.AGENTCORE_RUNTIME_URL;
  if (!runtimeUrl) {
    return Response.json(
      { error: "AGENTCORE_RUNTIME_URL is not configured" },
      { status: 503 },
    );
  }

  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return Response.json({ error: "request too large" }, { status: 413 });
  }

  // AgentCore groups traces into its Agents/Sessions observability views by
  // this header, not by the AG-UI protocol's own threadId in the JSON body —
  // without it, invocations still work but never appear under a session.
  // See docs/deployment.md#observability.
  const headers: Record<string, string> = {
    Authorization: authorization,
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const threadId = (() => {
    try {
      const parsed: unknown = JSON.parse(body);
      const value =
        parsed && typeof parsed === "object" && "threadId" in parsed
          ? (parsed as { threadId: unknown }).threadId
          : undefined;
      return typeof value === "string" && value.length > 0 ? value : undefined;
    } catch {
      return undefined;
    }
  })();
  if (threadId) {
    headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = threadId;
  }

  let upstream: Response;
  try {
    upstream = await fetch(runtimeUrl, {
      method: "POST",
      headers,
      body,
      signal: request.signal,
      cache: "no-store",
    });
  } catch {
    return Response.json({ error: "agent runtime unreachable" }, { status: 502 });
  }

  if (!upstream.ok || !upstream.body) {
    // Do not relay upstream error bodies; they can carry internal detail.
    const status = upstream.status === 401 || upstream.status === 403
      ? upstream.status
      : 502;
    return Response.json({ error: "agent runtime request failed" }, { status });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type":
        upstream.headers.get("content-type") ?? "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
    },
  });
}
