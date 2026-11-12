"""Generated texts follow PR_EXPLORER_LANG (default zh-Hant, like the UI); the language is part of both cache keys and
an earlier map donates its texts only when they are already in that language."""
import pytest

from pr_explorer.application.pipeline.stages import PipelineConfig
from pr_explorer.application.ports import ResolvedPr
from pr_explorer.application.use_cases import cache_key, system_map_key
from pr_explorer.bootstrap import output_language_from_env
from pr_explorer.domain.services.map_reuse import donor_language

PR = ResolvedPr(source="github", repo_key="gh-acme-shop-0", repo_name="shop", repo_owner="acme", base_ref="main",
                head_ref="f", base_sha="a" * 40, head_sha="b" * 40, merge_base_sha="a" * 40, title="Add discount codes",
                body="English body")


def test_default_is_zh_hant_and_env_overrides(monkeypatch):
    monkeypatch.delenv("PR_EXPLORER_LANG")
    assert output_language_from_env() == "zh-Hant"
    monkeypatch.setenv("PR_EXPLORER_LANG", "en")
    assert output_language_from_env() == "en"
    monkeypatch.setenv("PR_EXPLORER_LANG", "fr")
    with pytest.raises(ValueError):
        output_language_from_env()


def test_fixed_language_ignores_the_repo_text():
    assert PipelineConfig(output_language="zh-Hant").language("All English text here") == "zh-Hant"
    assert PipelineConfig(output_language="auto").language("All English text here") == "en"


def test_language_is_part_of_both_cache_keys():
    assert cache_key(PR, "v", "standard", lang="zh-Hant") != cache_key(PR, "v", "standard", lang="en")
    zh, en = system_map_key(PR, "v", True, "zh-Hant"), system_map_key(PR, "v", True, "en")
    assert zh != en and zh.endswith("|semantic")  # donor lookup relies on this suffix


def test_donor_language_reads_the_claude_texts():
    zh = {"capabilities": [{"description": {"text": "顧客在結帳時輸入折扣碼，系統依折扣後金額計算總價並建立訂單。", "source": "semantic"}}]}
    en = {"capabilities": [{"description": {"text": "Customers enter a discount code at checkout.", "source": "semantic"}}]}
    assert donor_language(zh) == "zh-Hant" and donor_language(en) == "en"


def test_a_donor_in_another_language_is_not_reused(fresh_shop, tmp_path, monkeypatch):
    """English texts from an earlier map are not copied into a map that must be written in Chinese."""
    from conftest import local_request
    from fakes import RecordingRunner, SmartClaude
    from pr_explorer.bootstrap import build_container

    claude, home = SmartClaude(), tmp_path / "home"
    monkeypatch.setenv("PR_EXPLORER_LANG", "en")
    build_container(home=home, claude_runner=RecordingRunner(claude)).analyze.analyze_sync(local_request(fresh_shop, mode="standard"))
    monkeypatch.setenv("PR_EXPLORER_LANG", "zh-Hant")
    _, rec = build_container(home=home, claude_runner=RecordingRunner(claude)).analyze.analyze_sync(
        local_request(fresh_shop, mode="standard"))
    assert len(claude.map_digests) == 2 and len(claude.map_digests[1]["capabilities"]) == 7  # asked again, nothing reused
