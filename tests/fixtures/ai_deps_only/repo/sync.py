"""Nightly sync job. Depends on openai in requirements but never imports it —
the classic dormant AI dependency (SPEC-3 ai_signals_only case)."""

import requests

API = "https://internal-api.example.com/v2/customers"


def fetch_customers(page: int) -> list[dict]:
    resp = requests.get(API, params={"page": page}, timeout=30)
    resp.raise_for_status()
    return resp.json()["results"]


def main() -> None:
    page = 1
    while True:
        batch = fetch_customers(page)
        if not batch:
            break
        for customer in batch:
            print(customer["id"])
        page += 1


if __name__ == "__main__":
    main()
