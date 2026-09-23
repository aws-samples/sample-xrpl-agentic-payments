"use client";

import { HttpAgent, randomUUID, type Message, type ToolCall } from "@ag-ui/client";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AGENT_ID,
  FINANCIAL_TOOLS,
  TOOL_RESULTS,
  quoteCardArgs,
  transferIdArg,
} from "@/lib/agui";
import { isFinancialComponent } from "@/lib/financial-components";
import { useAgentSession } from "./agent-session";
import { QuoteCard, TransferCard } from "./transfer-card";

const INSTRUCTIONS =
  "Use the governed tools and application-owned cards. Never approve a transfer for the user.";

// A render_* call halts the Runtime's stream. The UI answers it and lets the
// agent continue, but only a few times per user message.
const MAX_FOLLOW_UPS = 2;

function messageText(message: Message): string {
  if (!("content" in message)) return "";
  const { content } = message;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => ("text" in part && typeof part.text === "string" ? part.text : ""))
    .join("");
}

function unansweredRenderCalls(messages: readonly Message[]): ToolCall[] {
  const answered = new Set(
    messages.flatMap((message) =>
      message.role === "tool" ? [message.toolCallId] : [],
    ),
  );
  return messages.flatMap((message) =>
    message.role === "assistant"
      ? (message.toolCalls ?? []).filter(
          (call) =>
            isFinancialComponent(call.function.name) && !answered.has(call.id),
        )
      : [],
  );
}

function FinancialComponentView({ call }: { call: ToolCall }) {
  const name = call.function.name;
  // Only allowlisted, application-owned components render; anything else the
  // model names is dropped rather than shown.
  if (!isFinancialComponent(name)) return null;
  if (name === "render_quote_card") {
    const args = quoteCardArgs(call.function.arguments);
    return args ? <QuoteCard {...args} /> : null;
  }
  const transferId = transferIdArg(call.function.arguments);
  return transferId ? <TransferCard transferId={transferId} /> : null;
}

function ChatMessage({ message }: { message: Message }) {
  if (message.role !== "user" && message.role !== "assistant") return null;
  const text = messageText(message);
  return (
    <>
      {text && (
        <div className={`chat-bubble chat-bubble-${message.role}`}>{text}</div>
      )}
      {message.role === "assistant" &&
        message.toolCalls?.map((call) => (
          <FinancialComponentView key={call.id} call={call} />
        ))}
    </>
  );
}

export function AgentExperience() {
  const { runtimeUrl, headers, forwardedProps } = useAgentSession();
  const demoRecipientAddress =
    process.env.NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS?.trim() ?? "";
  const demoPayoutAlias =
    process.env.NEXT_PUBLIC_DEMO_PAYOUT_ALIAS?.trim() ||
    "fixture-bank-token";

  const agent = useMemo(
    () =>
      new HttpAgent({ agentId: AGENT_ID, url: runtimeUrl, threadId: randomUUID() }),
    [runtimeUrl],
  );
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    agent.headers = headers;
  }, [agent, headers]);

  useEffect(() => {
    const subscription = agent.subscribe({
      onMessagesChanged: ({ messages: next }) => setMessages([...next]),
    });
    return () => {
      subscription.unsubscribe();
      agent.abortRun();
    };
  }, [agent]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages, running]);

  const send = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content || agent.isRunning) return;
      setDraft("");
      setError(null);
      setRunning(true);
      agent.addMessage({ id: randomUUID(), role: "user", content });
      setMessages([...agent.messages]);
      try {
        for (let turn = 0; turn <= MAX_FOLLOW_UPS; turn += 1) {
          await agent.runAgent({
            tools: [...FINANCIAL_TOOLS],
            context: [{ description: "Chat instructions", value: INSTRUCTIONS }],
            forwardedProps,
          });
          const pending = unansweredRenderCalls(agent.messages);
          if (pending.length === 0) break;
          agent.addMessages(
            pending.map((call) => ({
              id: randomUUID(),
              role: "tool" as const,
              toolCallId: call.id,
              content: isFinancialComponent(call.function.name)
                ? TOOL_RESULTS[call.function.name]
                : "",
            })),
          );
        }
      } catch {
        setError("The assistant could not complete that request. Try again.");
      } finally {
        setMessages([...agent.messages]);
        setRunning(false);
      }
    },
    [agent, forwardedProps],
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send(draft);
  };

  const suggestions = [
    {
      title: "Direct wallet",
      message: demoRecipientAddress
        ? `Quote exactly 5 MXN from the usd-mxn-testnet corridor for direct XRPL wallet payout to ${demoRecipientAddress}. The recipient is Demo Recipient in MX, with 100 bps maximum slippage and no destination tag. Return the structured quote only; do not create a transfer intent until I confirm.`
        : "I want a 5 MXN quote from the usd-mxn-testnet corridor for a direct XRPL wallet payout. Ask me for the recipient's XRPL Testnet classic address before requesting the quote.",
    },
    {
      title: "Simulated fiat",
      message: `Quote exactly 10 MXN from the usd-mxn-testnet corridor for simulated local-fiat payout using payout alias ${demoPayoutAlias}. The recipient is Demo Recipient in MX, with 100 bps maximum slippage. Return the structured quote only; do not create a transfer intent until I confirm.`,
    },
  ];

  return (
    <div className="transfer-layout">
      <section className="hero-panel">
        <p className="eyebrow">Consumer remittance POC</p>
        <h1>Move value across borders with a governed assistant.</h1>
        <p>
          Ask for a USD→MXN quote, choose direct wallet or simulated local
          payout, then approve the immutable transfer card yourself.
        </p>
        <div className="scope-chips">
          <span>Exact output</span>
          <span>Bounded SendMax</span>
          <span>XRP x402 fee</span>
          <span>Refresh-safe workflow</span>
        </div>
      </section>
      <section className="chat-panel" aria-label="Transfer assistant">
        <div className="chat-shell">
          <header className="chat-header">Transfer assistant</header>
          <div className="chat-log" role="log" aria-live="polite">
            <div className="chat-bubble chat-bubble-assistant">
              Tell me the destination amount and whether you want a direct XRPL
              wallet or simulated local-fiat payout.
            </div>
            {messages.map((message) => (
              <ChatMessage key={message.id} message={message} />
            ))}
            {running && <div className="chat-typing">Assistant is working…</div>}
            {error && <p className="error-text">{error}</p>}
            <div ref={endRef} />
          </div>
          {messages.length === 0 && (
            <div className="chat-suggestions">
              {suggestions.map((suggestion) => (
                <button
                  key={suggestion.title}
                  type="button"
                  className="quiet-button"
                  disabled={running}
                  onClick={() => void send(suggestion.message)}
                >
                  {suggestion.title}
                </button>
              ))}
            </div>
          )}
          <form className="chat-input" onSubmit={submit}>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Quote 500 MXN to my Testnet wallet…"
              aria-label="Message the transfer assistant"
              disabled={running}
            />
            <button className="primary-button" disabled={running || !draft.trim()}>
              Send
            </button>
          </form>
        </div>
      </section>
    </div>
  );
}
