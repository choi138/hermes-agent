"""Register-validation preservation and introduced-failure controls."""
import pytest

from tools.delegation_progress_rewrite import validation_error


@pytest.mark.parametrize('text', [
    '추가 확인이 필요. 실행 기록은 있어.',
    '설정 확인은 중요. 아직 결과는 없어.',
])
def test_d_neutral_nominal_yo_is_not_a_polite_ending(text):
    assert validation_error(text, text) is None


@pytest.mark.parametrize('text', [
    '명령 실행은 시작됐어요. 아직 결과는 없어요.',
    '명령 실행은 시작됐어. 아직 결과는 없어.',
    '명령 실행 시작. 결과 미확인.',
    '명령이 실행돼요. 결과를 확인할게요.',
])
def test_d_consistent_or_neutral_register_is_allowed(text):
    assert validation_error(text, text) is None
