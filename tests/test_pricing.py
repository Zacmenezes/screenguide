import json
from pathlib import Path

import pytest

from screenguide.cli import main
from screenguide.config import ConfigError
from screenguide.pricing import ModelPrice, PricingTable, load_pricing

EXAMPLE = Path(__file__).parent.parent / "pricing.example.toml"
TABLE = PricingTable({("anthropic", "m"): ModelPrice(2.0, 10.0)})


def test_cost_comes_from_the_table():
    assert TABLE.cost("anthropic", "m", 1_000_000, 100_000) == pytest.approx(3.0)


def test_model_missing_from_the_table_has_unknown_cost():
    assert TABLE.cost("anthropic", "other", 1000, 100) is None


def test_same_model_name_under_another_provider_has_unknown_cost():
    assert TABLE.cost("openai", "m", 1000, 100) is None


@pytest.mark.parametrize("tokens", [(None, 100), (1000, None), (None, None)])
def test_missing_token_count_has_unknown_cost(tokens):
    assert TABLE.cost("anthropic", "m", *tokens) is None


@pytest.mark.parametrize("tokens", [(1000, 100), (None, None)])
def test_ollama_costs_nothing_without_a_table_entry(tokens):
    assert PricingTable({}).cost("ollama", "llama3", *tokens) == 0.0


def test_missing_pricing_file_is_an_empty_table(tmp_path):
    assert load_pricing(tmp_path / "absent.toml").cost("anthropic", "m", 1, 1) is None


def test_example_is_a_valid_table_with_the_default_model():
    cost = load_pricing(EXAMPLE).cost("anthropic", "claude-haiku-5-5", 1_000_000, 0)
    assert cost == pytest.approx(0.1)


@pytest.mark.parametrize(
    "content",
    [
        "a = 1\n",
        "[a]\nb = 1\n",
        "[a.b]\ninput_usd_per_mtok = 1\n",
        '[a.b]\ninput_usd_per_mtok = "1"\noutput_usd_per_mtok = 1\n',
        "[a.b]\ninput_usd_per_mtok = true\noutput_usd_per_mtok = 1\n",
        "[a.b]\ninput_usd_per_mtok = -1\noutput_usd_per_mtok = 1\n",
    ],
)
def test_malformed_table_is_rejected(tmp_path, content):
    path = tmp_path / "pricing.toml"
    path.write_text(content)

    with pytest.raises(ConfigError, match="pricing.toml"):
        load_pricing(path)


def test_cli_run_prices_the_record_from_the_user_table(home, image, usage_log):
    pricing = home / ".config" / "screenguide" / "pricing.toml"
    pricing.parent.mkdir(parents=True)
    pricing.write_text('[stub."stub"]\ninput_usd_per_mtok = 1.0\noutput_usd_per_mtok = 5.0\n')

    assert main(["run", "--image", str(image), "--goal", "Open settings"]) == 0

    # 1000 input tokens at $1/MTok plus 100 output tokens at $5/MTok.
    assert json.loads(usage_log.read_text())["cost_usd"] == pytest.approx(0.0015)
