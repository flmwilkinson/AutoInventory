"""Document review over an Azure OpenAI deployment.

The foundation model is not in the code: only the deployment name appears
in the URL path.
"""

import os

import requests

AZURE_BASE = "https://bankresource.openai.azure.com"


def review_document(text: str) -> str:
    url = AZURE_BASE + "/openai/deployments/prod-gpt4/chat/completions?api-version=2024-06-01"
    resp = requests.post(
        url,
        headers={"api-key": os.environ["AZURE_OPENAI_KEY"]},
        json={
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 400,
        },
    )
    return resp.json()["choices"][0]["message"]["content"]
