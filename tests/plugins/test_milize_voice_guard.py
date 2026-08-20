"""Regression tests for the default-profile Milize Korean voice guard.

The fixture is synthetic.  It preserves the failure shape observed in a long
research answer (formal endings only) without copying any user transcript.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


FORMAL_RESEARCH_FIXTURE = """\
## 시장 분석

현재 가격은 101.25달러입니다.
20일 이동평균은 98.40달러입니다.
단기 변동성은 17.2%로 관찰됩니다.
기준 시나리오의 핵심 위험은 거래량 감소입니다.
`risk_limit = 0.08` 설정을 유지하는 것이 필요합니다.
세부 근거는 https://example.com/research?id=20260729 에 있습니다.

```python
risk_limit = 0.08
symbol = "TEST"
```
"""

WARM_RESEARCH_FIXTURE = """\
## 시장 분석

현재 가격은 101.25달러예요.
20일 이동평균은 98.40달러예요.
단기 변동성은 17.2%로 보여요.
기준 시나리오의 핵심 위험은 거래량 감소예요.
`risk_limit = 0.08` 설정을 유지하는 게 좋아요.
세부 근거는 https://example.com/research?id=20260729 에 있어요.

```python
risk_limit = 0.08
symbol = "TEST"
```
"""

DETERMINISTIC_RESEARCH_FIXTURE = """\
## 시장 분석

현재 가격은 101.25달러예요.
20일 이동평균은 98.40달러예요.
단기 변동성은 17.2%로 관찰돼요.
기준 시나리오의 핵심 위험은 거래량 감소예요.
`risk_limit = 0.08` 설정을 유지하는 것이 필요해요.
세부 근거는 https://example.com/research?id=20260729 에 있어요.

```python
risk_limit = 0.08
symbol = "TEST"
```
"""

UNSUPPORTED_FORMAL_FIXTURE = """\
## 식단

아침에는 사과 1개를 먹습니다.
점심에는 현미밥을 먹습니다.
간식에는 견과류를 먹습니다.
저녁에는 채소를 먹습니다.
운동 뒤에는 바나나를 먹습니다.
취침 전에는 아무것도 먹습니다.
"""

UNSUPPORTED_WARM_FIXTURE = UNSUPPORTED_FORMAL_FIXTURE.replace("먹습니다", "먹어요")


def _load_guard():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "milize-voice-guard"
    module_name = "hermes_plugins.milize_voice_guard"
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        sys.modules["hermes_plugins"] = namespace
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLlm:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


def test_long_form_formal_fixture_triggers_guard():
    guard = _load_guard()

    metrics = guard.analyze_style(FORMAL_RESEARCH_FIXTURE)

    assert metrics.sentence_count >= 6
    assert metrics.formal_count >= 6
    assert metrics.haeyo_count == 0
    assert metrics.max_formal_run >= 3
    assert metrics.formal_share == pytest.approx(1.0)
    assert metrics.should_repair is True


def test_warm_long_form_does_not_trigger_guard():
    guard = _load_guard()

    metrics = guard.analyze_style(WARM_RESEARCH_FIXTURE)

    assert metrics.haeyo_count >= 6
    assert metrics.should_repair is False


@pytest.mark.parametrize(
    "user_request",
    [
        "공식 보고서 형식의 격식체로 작성해줘",
        "안전 경고문을 하십시오체로 작성해 주세요",
        "Write this as a formal incident report.",
    ],
)
def test_explicit_formal_requests_are_exempt(user_request):
    guard = _load_guard()
    assert guard.explicit_formal_intent(user_request) is True


def test_rewrite_validation_preserves_numbers_urls_code_and_markdown():
    guard = _load_guard()

    assert guard.validate_rewrite(
        FORMAL_RESEARCH_FIXTURE,
        WARM_RESEARCH_FIXTURE,
    ) is True
    assert guard.validate_rewrite(
        FORMAL_RESEARCH_FIXTURE,
        WARM_RESEARCH_FIXTURE.replace("101.25", "109.25", 1),
    ) is False
    assert guard.validate_rewrite(
        FORMAL_RESEARCH_FIXTURE,
        WARM_RESEARCH_FIXTURE.replace('symbol = "TEST"', 'symbol = "FAIL"'),
    ) is False
    assert guard.validate_rewrite(
        FORMAL_RESEARCH_FIXTURE,
        WARM_RESEARCH_FIXTURE.replace("## 시장 분석", "### 시장 분석"),
    ) is False


def test_deterministic_repair_changes_only_supported_sentence_endings():
    guard = _load_guard()

    candidate = guard.deterministic_repair(FORMAL_RESEARCH_FIXTURE)

    assert candidate == DETERMINISTIC_RESEARCH_FIXTURE
    assert guard.validate_rewrite(FORMAL_RESEARCH_FIXTURE, candidate) is True


def test_guard_adds_late_contract_and_repairs_once_for_default_discord():
    guard_mod = _load_guard()
    llm = _FakeLlm(WARM_RESEARCH_FIXTURE)
    voice_guard = guard_mod.VoiceGuard(llm=llm, profile_name="default")

    contract = voice_guard.pre_llm(
        session_id="session-1",
        platform="discord",
        user_message="시장 상황을 자세히 분석해줘",
    )
    transformed = voice_guard.transform(
        session_id="session-1",
        platform="discord",
        response_text=FORMAL_RESEARCH_FIXTURE,
        model="test/model",
    )

    assert "따뜻한 해요체" in contract
    assert transformed == DETERMINISTIC_RESEARCH_FIXTURE
    assert len(llm.calls) == 0


def test_guard_uses_one_llm_fallback_for_unsupported_formal_ending():
    guard_mod = _load_guard()
    llm = _FakeLlm(UNSUPPORTED_WARM_FIXTURE)
    voice_guard = guard_mod.VoiceGuard(llm=llm, profile_name="default")

    voice_guard.pre_llm(
        session_id="fallback-session",
        platform="discord",
        user_message="식단을 자세히 설명해줘",
    )
    transformed = voice_guard.transform(
        session_id="fallback-session",
        platform="discord",
        response_text=UNSUPPORTED_FORMAL_FIXTURE,
    )

    assert transformed == UNSUPPORTED_WARM_FIXTURE
    assert len(llm.calls) == 1
    assert llm.calls[0]["temperature"] == 0


def test_guard_does_not_run_for_other_profiles_or_platforms():
    guard_mod = _load_guard()

    other_profile_llm = _FakeLlm(WARM_RESEARCH_FIXTURE)
    other_profile = guard_mod.VoiceGuard(
        llm=other_profile_llm,
        profile_name="shinei",
    )
    assert other_profile.pre_llm(
        session_id="other-profile",
        platform="discord",
        user_message="분석해줘",
    ) is None
    assert other_profile.transform(
        session_id="other-profile",
        platform="discord",
        response_text=FORMAL_RESEARCH_FIXTURE,
    ) is None
    assert other_profile_llm.calls == []

    cli_llm = _FakeLlm(WARM_RESEARCH_FIXTURE)
    cli_guard = guard_mod.VoiceGuard(llm=cli_llm, profile_name="default")
    assert cli_guard.pre_llm(
        session_id="cli-session",
        platform="cli",
        user_message="분석해줘",
    ) is None
    assert cli_guard.transform(
        session_id="cli-session",
        platform="cli",
        response_text=FORMAL_RESEARCH_FIXTURE,
    ) is None
    assert cli_llm.calls == []


def test_explicit_formal_request_skips_repair():
    guard_mod = _load_guard()
    llm = _FakeLlm(WARM_RESEARCH_FIXTURE)
    voice_guard = guard_mod.VoiceGuard(llm=llm, profile_name="default")

    voice_guard.pre_llm(
        session_id="formal-session",
        platform="discord",
        user_message="공식 보고서 형식의 격식체로 작성해줘",
    )
    transformed = voice_guard.transform(
        session_id="formal-session",
        platform="discord",
        response_text=FORMAL_RESEARCH_FIXTURE,
    )

    assert transformed is None
    assert llm.calls == []


def test_semantic_anchor_drift_fails_open_to_original_response():
    guard_mod = _load_guard()
    changed_number = UNSUPPORTED_WARM_FIXTURE.replace("1개", "2개", 1)
    llm = _FakeLlm(changed_number)
    voice_guard = guard_mod.VoiceGuard(llm=llm, profile_name="default")

    voice_guard.pre_llm(
        session_id="drift-session",
        platform="discord",
        user_message="시장 상황을 자세히 분석해줘",
    )
    transformed = voice_guard.transform(
        session_id="drift-session",
        platform="discord",
        response_text=UNSUPPORTED_FORMAL_FIXTURE,
    )

    assert transformed is None
    assert len(llm.calls) == 1
