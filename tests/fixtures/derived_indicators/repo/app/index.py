"""Nightly embedding job (plain model usage, not an agent)."""

from openai import OpenAI

client = OpenAI()


def embed_documents(texts: list) -> list:
    resp = client.embeddings.create(model="text-embedding-3-small", input=list(texts))
    return [d.embedding for d in resp.data]
