// Top-level side effects that must NEVER run — aiscan only parses, never
// executes. If any of these lines run, the canary file appears and the
// no-exec test fails.
import * as fs from "fs";
import OpenAI from "openai";

fs.writeFileSync("CANARY_TOPLEVEL", "pwned");

const client = new OpenAI();

export async function run(q: string) {
  const r = await client.chat.completions.create({
    model: "gpt-4o",
    messages: [{ role: "user", content: q }],
  });
  return r.choices[0].message.content;
}

// Immediately-invoked side effect at module load.
(() => {
  fs.writeFileSync("CANARY_IIFE", "pwned");
})();
