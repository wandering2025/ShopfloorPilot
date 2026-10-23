"""S09-A07/A08/A10: explicit configuration, redaction, and CLI gating."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from shopfloor_pilot.cli import main
from shopfloor_pilot.domain.fixtures import load_scenario, project_public_view
from shopfloor_pilot.providers.config import (
    load_env_file,
    load_paid_run_settings,
)
from shopfloor_pilot.providers.deepseek import build_outbound_payload
from shopfloor_pilot.providers.errors import (
    ConfigurationError,
    ProviderError,
    ProviderErrorCode,
)
from shopfloor_pilot.providers.models import (
    MessageRole,
    ProviderMessage,
    ProviderRequest,
    ProviderToolCall,
)

SECRET = "sentinel-credential-do-not-log-0123456789"


def _base_environment() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": SECRET,
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "DEEPSEEK_THINKING": "disabled",
        "SHOPFLOOR_PROVIDER_CURRENCY": "USD",
        "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET": "0.01",
        "SHOPFLOOR_PROVIDER_RUN_BUDGET": "0.05",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE": "0.006",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE": "0.30",
        "SHOPFLOOR_PROVIDER_OUTPUT_RATE": "1.20",
        "SHOPFLOOR_PROVIDER_TARIFF_DATE": "2026-10-21",
        "SHOPFLOOR_PROVIDER_TARIFF_SOURCE": ("https://api-docs.deepseek.com/quick_start/pricing"),
    }


def test_complete_paid_configuration_and_secret_representation() -> None:
    settings = load_paid_run_settings(_base_environment())
    assert settings.api_key == SECRET
    assert settings.currency == "USD"
    assert settings.workflow_cap_micros == 10_000
    assert settings.run_cap_micros == 50_000
    assert settings.pricing.input_cache_miss_per_million == Decimal("0.30")
    assert SECRET not in repr(settings)
    assert SECRET not in repr(settings.connection)
    assert SECRET not in json.dumps(settings.public_dict())


@pytest.mark.parametrize(
    "missing_name",
    [
        "DEEPSEEK_MODEL",
        "SHOPFLOOR_PROVIDER_CURRENCY",
        "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET",
        "SHOPFLOOR_PROVIDER_RUN_BUDGET",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE",
        "SHOPFLOOR_PROVIDER_OUTPUT_RATE",
        "SHOPFLOOR_PROVIDER_TARIFF_DATE",
        "SHOPFLOOR_PROVIDER_TARIFF_SOURCE",
    ],
)
def test_missing_paid_setting_fails_before_request(missing_name: str) -> None:
    environment = _base_environment()
    environment.pop(missing_name)
    with pytest.raises(ConfigurationError) as exc:
        load_paid_run_settings(environment)
    assert missing_name in exc.value.message
    assert exc.value.retryable is False


def test_missing_key_names_both_alternatives() -> None:
    environment = _base_environment()
    environment.pop("DEEPSEEK_API_KEY")
    with pytest.raises(ConfigurationError) as exc:
        load_paid_run_settings(environment)
    assert "DEEPSEEK_API_KEY" in exc.value.message
    assert "DEEPSEEK_API_KEY_FILE" in exc.value.message


def test_key_file_is_read_without_echo(tmp_path: Path) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text(SECRET + "\n", encoding="utf-8")
    environment = _base_environment()
    environment.pop("DEEPSEEK_API_KEY")
    environment["DEEPSEEK_API_KEY_FILE"] = str(key_file)
    settings = load_paid_run_settings(environment)
    assert settings.api_key == SECRET
    assert SECRET not in repr(settings)
    assert str(key_file) not in repr(settings)


def test_provider_env_file_is_restricted_and_quoted(tmp_path: Path) -> None:
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "export DEEPSEEK_MODEL=deepseek-flash",
                'SHOPFLOOR_PROVIDER_CURRENCY="USD"',
                "UNRELATED_SETTING=ignored",
            ]
        ),
        encoding="utf-8",
    )
    values = load_env_file(env_file)
    assert values == {
        "DEEPSEEK_MODEL": "deepseek-flash",
        "SHOPFLOOR_PROVIDER_CURRENCY": "USD",
    }
    assert "UNRELATED_SETTING" not in values


def test_error_redaction_covers_known_and_pattern_secrets() -> None:
    error = ProviderError(
        ProviderErrorCode.CONNECTION,
        f"failure with {SECRET} and Bearer {SECRET}",
        secrets=(SECRET,),
    )
    assert SECRET not in str(error)
    serialized = json.dumps(error.as_dict())
    assert SECRET not in serialized
    assert "Bearer" not in serialized
    assert "[REDACTED]" in serialized


def test_outbound_payload_has_only_approved_projection() -> None:
    bundle = load_scenario("machine_breakdown")
    evaluator_marker = bundle.evaluator.fault_truth[0].cause_label
    assert evaluator_marker == "EVAL_ONLY_SENTINEL_SPINDLE_BEARING_DAMAGE"
    public_view = project_public_view(bundle, simulated_time_seconds=2700)
    public_json = public_view.model_dump_json()
    settings = load_paid_run_settings(_base_environment()).connection
    request = ProviderRequest(
        messages=(
            ProviderMessage(role=MessageRole.SYSTEM, content="bounded system prompt"),
            ProviderMessage(role=MessageRole.USER, content=public_json),
        ),
        tools=(),
        max_output_tokens=64,
    )
    payload = build_outbound_payload(request, settings)
    serialized = json.dumps(payload)
    assert set(payload) == {"model", "messages", "max_tokens", "stream", "thinking"}
    assert public_view.scenario_id in serialized
    assert public_view.observations[0].summary in serialized
    assert SECRET not in serialized
    assert evaluator_marker not in public_json
    assert evaluator_marker not in serialized
    assert evaluator_marker not in request.estimated_payload_text()
    assert "fault_truth" not in serialized
    assert "interventions" not in serialized


def test_reasoning_only_in_wire_projection_not_audit() -> None:
    settings = load_paid_run_settings(_base_environment()).connection
    assistant = ProviderMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=(
            ProviderToolCall(
                id="call-1",
                name="fixture_machine_status",
                arguments_json='{"machine_id":"M1"}',
            ),
        ),
        reasoning="hidden-reasoning",
    )
    request = ProviderRequest(
        messages=(
            ProviderMessage(role=MessageRole.USER, content="check"),
            assistant,
            ProviderMessage(role=MessageRole.TOOL, content="{}", tool_call_id="call-1"),
        ),
        max_output_tokens=16,
    )
    payload = build_outbound_payload(request, settings)
    assert payload["messages"][1]["reasoning_content"] == "hidden-reasoning"
    assert "hidden-reasoning" not in json.dumps(assistant.public_dict())


def test_cli_offline_check_labels_fake_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "offline-readiness"
    code = main(["provider-check", "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads((output / "readiness.json").read_text(encoding="utf-8"))
    assert report["status"] == "OFFLINE_ONLY"
    assert report["evidence_kind"] == "offline_contract_evidence"
    assert report["provider"] == "scripted-fake"
    assert report["live_readiness"] is False
    assert "OFFLINE CONTRACT EVIDENCE" in report["label"]
    printed = json.loads(captured.out)
    assert printed["status"] == "OFFLINE_ONLY"
    summary = (output / "SUMMARY.md").read_text(encoding="utf-8")
    assert "not live-readiness evidence" in summary


def test_offline_cli_does_not_echo_environment_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    output = tmp_path / "offline-secret-check"
    assert main(["provider-check", "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    for path in output.iterdir():
        assert SECRET not in path.read_text(encoding="utf-8")


def test_cli_live_without_configuration_is_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY_FILE",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "SHOPFLOOR_PROVIDER_CURRENCY",
        "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET",
        "SHOPFLOOR_PROVIDER_RUN_BUDGET",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE",
        "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE",
        "SHOPFLOOR_PROVIDER_OUTPUT_RATE",
        "SHOPFLOOR_PROVIDER_TARIFF_DATE",
        "SHOPFLOOR_PROVIDER_TARIFF_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("# intentionally empty\n", encoding="utf-8")
    output = tmp_path / "live-skipped"
    code = main(
        [
            "provider-check",
            "--live",
            "--output",
            str(output),
            "--env-file",
            str(empty_env_file),
        ]
    )
    assert code == 2
    report = json.loads((output / "readiness.json").read_text(encoding="utf-8"))
    assert report["status"] == "SKIPPED"
    assert report["live_readiness"] is False
    assert "missing" in report
    assert "DEEPSEEK_MODEL" in report["reason"]
    assert "DEEPSEEK_API_KEY" in report["reason"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "SKIPPED"


def test_workflow_budget_cannot_exceed_run_budget() -> None:
    environment = _base_environment()
    environment["SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET"] = "0.06"
    environment["SHOPFLOOR_PROVIDER_RUN_BUDGET"] = "0.05"
    with pytest.raises(ConfigurationError):
        load_paid_run_settings(environment)


def test_base_url_rejects_embedded_credentials() -> None:
    environment = _base_environment()
    environment["DEEPSEEK_BASE_URL"] = "https://user:pass@example.com"
    with pytest.raises(ConfigurationError):
        load_paid_run_settings(environment)


@pytest.mark.parametrize(
    "base_url",
    (
        "https://example.com",
        "https://api.deepseek.com.evil.example",
        "https://api.deepseek.com:443",
        "https://api.deepseek.com/beta",
        "https://api.deepseek.com/v1",
    ),
)
def test_base_url_rejects_non_official_or_nonstandard_endpoint(base_url: str) -> None:
    environment = _base_environment()
    environment["DEEPSEEK_BASE_URL"] = base_url
    with pytest.raises(ConfigurationError):
        load_paid_run_settings(environment)
