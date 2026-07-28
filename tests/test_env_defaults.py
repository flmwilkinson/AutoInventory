"""SPEC-5 §6: config-at-rest env defaults — dotenv/compose/k8s parsing,
secret redaction, example-vs-pinned forms."""

from __future__ import annotations

from pathlib import Path

from aiscan.ingest.env_defaults import collect_env_defaults


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestCollectEnvDefaults:
    def test_dotenv_example_and_forms(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "infra/env.example",
            'MODEL_DEFAULT="gpt-4o"\nMODEL_FAST=gpt-4o-mini # inline comment\n',
        )
        _write(tmp_path, ".env", "MODEL_FAST=azure.gpt-4o-mini\n")
        out = collect_env_defaults(tmp_path)
        assert [d.value for d in out["MODEL_DEFAULT"]] == ["gpt-4o"]
        assert out["MODEL_DEFAULT"][0].form == "example"
        fast = {(d.value, d.form) for d in out["MODEL_FAST"]}
        assert fast == {("gpt-4o-mini", "example"), ("azure.gpt-4o-mini", "pinned")}

    def test_compose_and_k8s_env(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "docker-compose.yml",
            "services:\n  worker:\n    environment:\n      MODEL_DEFAULT: gpt-4o\n",
        )
        _write(
            tmp_path,
            "infra/deploy.yaml",
            (
                "spec:\n  containers:\n    - name: app\n      env:\n"
                "        - name: MODEL_FAST\n          value: gpt-4o-mini\n"
            ),
        )
        out = collect_env_defaults(tmp_path)
        assert out["MODEL_DEFAULT"][0].value == "gpt-4o"
        assert out["MODEL_DEFAULT"][0].form == "pinned"
        assert out["MODEL_FAST"][0].value == "gpt-4o-mini"
        assert out["MODEL_FAST"][0].source == "infra/deploy.yaml"

    def test_secret_shaped_values_redacted(self, tmp_path: Path) -> None:
        _write(tmp_path, ".env.example", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuv\n")
        out = collect_env_defaults(tmp_path)
        assert out["OPENAI_API_KEY"][0].value == "[redacted]"

    def test_unrelated_yaml_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path, "config/settings.yaml", "environment:\n  MODEL: x\n")
        assert collect_env_defaults(tmp_path) == {}
