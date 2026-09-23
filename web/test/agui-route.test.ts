import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/agui/route";

const request = (headers: Record<string, string> = {}) =>
  new NextRequest("http://localhost:3000/api/agui", {
    method: "POST",
    headers,
    body: JSON.stringify({ threadId: "t", runId: "r", messages: [] }),
  });

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("/api/agui proxy", () => {
  it("rejects requests without a bearer token", async () => {
    vi.stubEnv("AGENTCORE_RUNTIME_URL", "https://runtime.example/invocations");
    expect((await POST(request())).status).toBe(401);
  });

  it("fails closed when the runtime URL is not configured", async () => {
    vi.stubEnv("AGENTCORE_RUNTIME_URL", "");
    const response = await POST(request({ authorization: "Bearer token" }));
    expect(response.status).toBe(503);
  });

  it("forwards the bearer token to the configured runtime and streams back", async () => {
    vi.stubEnv("AGENTCORE_RUNTIME_URL", "https://runtime.example/invocations");
    const fetchMock = vi.fn(async () =>
      new Response("data: {}\n\n", {
        headers: { "content-type": "text/event-stream" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(request({ authorization: "Bearer token" }));

    expect(response.status).toBe(200);
    expect(await response.text()).toBe("data: {}\n\n");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://runtime.example/invocations");
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer token",
    );
  });

  it("does not relay upstream error bodies", async () => {
    vi.stubEnv("AGENTCORE_RUNTIME_URL", "https://runtime.example/invocations");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("internal trace", { status: 500 })),
    );
    const response = await POST(request({ authorization: "Bearer token" }));
    expect(response.status).toBe(502);
    expect(await response.text()).not.toContain("internal trace");
  });
});
