export async function lookupAccount(args: unknown): Promise<string> {
  const resp = await fetch("https://accounts.internal/lookup", {
    method: "GET",
  });
  return await resp.text();
}
