"""MemoryManager strips slash-skill scaffolding for every provider.

When a user invokes a /skill or /bundle, Hermes expands the turn into a
model-facing message that embeds the full skill body. Feeding that verbatim to
memory providers pollutes their stores/embeddings with prompt scaffolding
instead of what the user actually asked. The strip lives once in MemoryManager
so it covers the whole provider fan-out — not per backend.

See: agent.skill_commands.extract_user_instruction_from_skill_message and
MemoryManager._strip_skill_scaffolding.
"""

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message


_SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body that must not be searched or embedded.\n\n"
    "The user has provided the following instruction alongside the skill invocation: "
    "make a skill for release triage"
)

_BUNDLE_TURN = (
    '[IMPORTANT: The user has invoked the "backend-dev" skill bundle, '
    "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
    "Bundle: backend-dev\n"
    "Skills loaded: test-driven-development, code-review\n\n"
    "User instruction: fix the failing retrieval test\n\n"
    '[Loaded as part of the "backend-dev" skill bundle.]\n\n'
    "Large bundled skill body that must not be searched or embedded."
)

_BARE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body, no user instruction."
)

_TRIGGERING_PREFIX = (
    "[Triggering message id: `1542732296973647893` — use as `message_id` for "
    "reply/react/pin via the discord tools.]"
)
_REPLYING_PREFIX = '[Replying to: "the earlier deployment note"]'


class _RecordingProvider(MemoryProvider):
    """Captures exactly what user text each fan-out method received."""

    _name = "recording"

    def __init__(self):
        self.prefetched = []
        self.queued = []
        self.synced = []
        self.synced_assistant = []

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.prefetched.append(query)
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.queued.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        self.synced.append(user_content)
        self.synced_assistant.append(assistant_content)

    def get_tool_schemas(self):
        return []


def _manager_with_recorder():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


class TestExtractUserInstruction:
    def test_non_string_returns_none(self):
        assert extract_user_instruction_from_skill_message(None) is None
        assert extract_user_instruction_from_skill_message(123) is None
        assert extract_user_instruction_from_skill_message([{"text": "hi"}]) is None



    def test_bundle_with_instruction(self):
        assert (
            extract_user_instruction_from_skill_message(_BUNDLE_TURN)
            == "fix the failing retrieval test"
        )




class TestMemoryManagerStripsScaffolding:

    def test_prefetch_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        result = mgr.prefetch_all(_BARE_SKILL_TURN)
        assert result == ""
        assert provider.prefetched == []

    def test_queue_prefetch_all_strips_bundle(self):
        mgr, provider = _manager_with_recorder()
        mgr.queue_prefetch_all(_BUNDLE_TURN)
        mgr.flush_pending(timeout=5.0)
        assert provider.queued == ["fix the failing retrieval test"]



    def test_sync_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_BARE_SKILL_TURN, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == []


class TestMemoryManagerStripsGatewayPrefixes:
    def test_sync_all_strips_one_leading_prefix(self):
        prefixes = (
            _TRIGGERING_PREFIX,
            _REPLYING_PREFIX,
            '[Replying to your previous message: "the earlier deployment note"]',
            '[Routing directive: label=SYSTEM_DEV target=openai/gpt-5/high]',
            "[Note: model was just switched from model-a to model-b by the model "
            "router (route 'coding'). Adjust your self-identification accordingly.]",
        )
        for prefix in prefixes:
            mgr, provider = _manager_with_recorder()
            mgr.sync_all(f"{prefix}\n\nship the retrieval fix", "Done.")
            mgr.flush_pending(timeout=5.0)
            assert provider.synced == ["ship the retrieval fix"], prefix

    def test_sync_all_strips_multiple_leading_prefixes(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(
            f"{_TRIGGERING_PREFIX}\n\n{_REPLYING_PREFIX}\n\ncontinue the rollout",
            "Done.",
        )
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == ["continue the rollout"]

    def test_sync_all_preserves_prefix_shape_in_message_body(self):
        mgr, provider = _manager_with_recorder()
        user_content = f"Keep this example:\n{_TRIGGERING_PREFIX}\nIt is user-authored."
        mgr.sync_all(user_content, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == [user_content]

    def test_sync_all_preserves_plain_message(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all("ordinary user message", "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == ["ordinary user message"]

    def test_sync_all_strips_skill_scaffolding_then_gateway_prefix(self):
        mgr, provider = _manager_with_recorder()
        combined = _SINGLE_SKILL_TURN.replace(
            "make a skill for release triage",
            f"{_TRIGGERING_PREFIX}\n\nmake a skill for release triage",
        )
        mgr.sync_all(combined, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == ["make a skill for release triage"]

    def test_sync_all_skips_prefix_only_message(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_TRIGGERING_PREFIX, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == []

    def test_sync_all_does_not_strip_assistant_content(self):
        mgr, provider = _manager_with_recorder()
        assistant_content = f"{_TRIGGERING_PREFIX}\n\nassistant-authored example"
        mgr.sync_all("ordinary user message", assistant_content)
        mgr.flush_pending(timeout=5.0)
        assert provider.synced_assistant == [assistant_content]
