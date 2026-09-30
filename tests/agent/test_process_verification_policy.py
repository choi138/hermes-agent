"""Exercise the shared dispatch boundary and its propagated worker context."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
import pytest

@pytest.mark.parametrize('name,args', [
    ('terminal', {'command': 'touch forbidden'}), ('patch', {}),
    ('memory', {'action': 'add'}), ('delegate_task', {}), ('execute_code', {}),
    ('mcp_arbitrary_write', {}), ('process_manage', {'action': 'kill', 'session_id': 'proc_test'}),
])
def test_verification_lease_blocks_effects_before_plugins(monkeypatch, tmp_path, name, args):
    from tools import process_registry_followups as ledger
    from tools.thread_context import propagate_context_to_thread
    from agent.tool_executor import _run_agent_tool_execution_middleware
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = MagicMock()
    effect = tmp_path / 'forbidden'
    from agent import relay_tools
    def effectful_hook(name, args, next_handler, **kwargs):
        effect.touch()
        return next_handler(args), args
    monkeypatch.setattr(relay_tools, 'execute', effectful_hook)
    def execute(args):
        effect.touch()
        return '{}'
    with ledger.verification_lease('proc_test', 'token'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(propagate_context_to_thread(lambda:
                _run_agent_tool_execution_middleware(agent, function_name=name,
                    function_args=args, effective_task_id='test', tool_call_id='call', execute=execute))).result()
    assert result.blocked
    assert not effect.exists()

@pytest.mark.parametrize('mode', ['sequential', 'concurrent'])
def test_real_agent_dispatch_preserves_reads_and_blocks_repair(monkeypatch, tmp_path, mode):
    import json
    from types import SimpleNamespace
    from unittest.mock import patch
    from run_agent import AIAgent
    from agent import tool_executor
    from tools.process_registry_followups import verification_lease
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('terminal:\n  env: local\n')
    evidence = tmp_path / 'evidence.txt'
    evidence.write_text('verification-evidence')
    effect = tmp_path / 'forbidden'
    with patch('agent.process_bootstrap.OpenAI'), patch('agent.model_metadata.fetch_model_metadata', return_value={}):
        agent = AIAgent(api_key='test-only', base_url='http://127.0.0.1:1/v1', quiet_mode=True,
                        skip_context_files=True, skip_memory=True, enabled_toolsets=['file', 'terminal'])
    calls = []
    for name, args in [('read_file', {'path': str(evidence)}),
                       ('terminal', {'command': 'touch ' + str(effect)}),
                       ('memory', {'action': 'add', 'target': 'memory', 'content': 'forbidden'})]:
        calls.append(SimpleNamespace(id=name, type='function', function=SimpleNamespace(name=name, arguments=json.dumps(args))))
    messages = []
    with verification_lease('proc_test', 'token'):
        getattr(tool_executor, 'execute_tool_calls_' + mode)(agent, SimpleNamespace(tool_calls=calls), messages, 'test', finalize=False)
    results = {m['tool_call_id']: m['content'] for m in messages if m.get('role') == 'tool'}
    assert 'verification-evidence' in results['read_file']
    assert 'read-only' in results['terminal']
    assert 'read-only' in results['memory']
    assert not effect.exists()



def test_relay_rewrite_of_allowed_poll_cannot_dispatch_kill(monkeypatch, tmp_path):
    from tools.process_registry_followups import verification_lease
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from agent import relay_tools
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    hooks=[]; effects=[]
    def rewrite(name,args,next_handler,**kwargs):
        hooks.append(name)
        rewritten={**args,'action':'kill'}
        return next_handler(rewritten),rewritten
    monkeypatch.setattr(relay_tools,'execute',rewrite)
    with verification_lease('proc_test','token'):
        result=_run_agent_tool_execution_middleware(MagicMock(),function_name='process_manage',
            function_args={'action':'poll','session_id':'proc_test'},effective_task_id='test',
            tool_call_id='rewrite',execute=lambda args:effects.append(args) or '{}')
    assert hooks==['process_manage']
    assert result.blocked and not effects
