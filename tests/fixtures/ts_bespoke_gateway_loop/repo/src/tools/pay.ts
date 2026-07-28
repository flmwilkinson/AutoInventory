export async function sendPayment(args: unknown): Promise<string> {
  const resp = await fetch("https://payments-core.internal/transfer", {
    method: "POST",
    headers: { Authorization: "Bearer " + process.env.PAYMENTS_TOKEN },
    body: JSON.stringify(args),
  });
  return await resp.text();
}
