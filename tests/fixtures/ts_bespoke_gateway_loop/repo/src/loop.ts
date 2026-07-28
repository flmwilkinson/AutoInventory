// Headline TS case: a hand-rolled agent loop over an internal LLM gateway.
// No SDK, no known host — detected by request shape, model attributed.
import { lookupAccount } from "./tools/lookup";
import { sendPayment } from "./tools/pay";

const TOOLS: Record<string, (args: unknown) => unknown> = {
  lookup_account: lookupAccount,
  send_payment: sendPayment,
};

const SYSTEM_PROMPT = "You are the ops assistant. Help with account operations.";

export async function runAgent(question: string): Promise<string> {
  const messages: Array<Record<string, unknown>> = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: question },
  ];
  while (true) {
    const resp = await fetch("https://gw.internal.example/llm/v1/chat", {
      method: "POST",
      headers: { Authorization: "Bearer " + process.env.GATEWAY_TOKEN },
      body: JSON.stringify({
        model: "internal-x1",
        messages,
        tools: Object.keys(TOOLS),
      }),
    });
    const data = await resp.json();
    const choice = data.choices[0];
    if (choice.finish_reason !== "tool_calls") {
      return choice.message.content;
    }
    messages.push(choice.message);
    for (const call of choice.message.tool_calls) {
      const fn = TOOLS[call.function.name];
      messages.push({ role: "tool", content: fn(call.function.arguments) });
    }
  }
}
