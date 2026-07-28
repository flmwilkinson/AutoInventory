import { Agent } from "./sdk";

const spanish = new Agent({
  name: "Spanish agent",
  instructions: "You only respond in Spanish.",
  model: "gpt-4o",
});

export const triage = new Agent({
  name: "Triage agent",
  instructions: "Route the request to the right specialist.",
  model: "gpt-4o",
  handoffs: [spanish],
});
