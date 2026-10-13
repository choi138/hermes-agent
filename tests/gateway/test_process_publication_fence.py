"""Internal completion assessments cannot reach speech or fallback commentary."""
from types import SimpleNamespace
from unittest.mock import Mock

from gateway.run_turn_runner import TurnRunner


def test_foreground_fence_blocks_speech_and_fallback_commentary():
    speech = Mock()
    ctx = SimpleNamespace(mute_notification_reply=False, event=None,
        streaming_tts_consumer_holder=[speech], scheduled_heartbeat=False,
        resolve_display_setting=lambda *args: False, user_config={},
        interim_assistant_messages_enabled=True, _run_still_current=lambda: True,
        _status_adapter=Mock(), _status_thread_metadata={}, process_publication_deferred=False)
    runner = SimpleNamespace(config=None, _delivery_adapter_for=lambda source: None)
    ctx.source = SimpleNamespace()
    turn = TurnRunner(runner, ctx)
    turn._send_status_text = Mock()
    consumer, delta, commentary, _ = turn._setup_stream_consumer('discord')
    assert consumer is None
    delta('accepted preview')
    commentary('accepted commentary')
    before = speech.on_delta.call_count
    ctx.process_publication_deferred = True
    delta('```process_reports\nprivate assessment')
    commentary('private assessment')
    assert speech.on_delta.call_count == before
    assert turn._send_status_text.call_count == 1
