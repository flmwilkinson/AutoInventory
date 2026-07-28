// A TypeScript agent loop — now analysed end-to-end (SPEC-4).
import OpenAI from "openai";

const client = new OpenAI();

export async function runEvidenceAgent(question: string): Promise<string> {
  const messages: any[] = [
    { role: "system", content: "You gather evidence for document generation." },
    { role: "user", content: question },
  ];
  while (true) {
    const resp = await client.chat.completions.create({
      model: "gpt-4o",
      messages,
      tools: [],
    });
    const choice = resp.choices[0];
    if (choice.finish_reason !== "tool_calls") {
      return choice.message.content ?? "";
    }
    messages.push(choice.message);
  }
}
