from gateway.agent_health import AlertBudget, HealthEvent


def _event(rule="A.output_silence", session="s1"):
    return HealthEvent(
        rule=rule,
        category=rule[0],
        title="test",
        reason="test",
        session_key=session,
    )


def test_budget_applies_rule_session_cooldown():
    budget = AlertBudget(cooldown_seconds=900, hourly_cap=12)
    assert budget.admit(_event(), now=0) is not None
    assert budget.admit(_event(), now=899) is None
    next_admitted = budget.admit(_event(session="s2"), now=899)
    assert next_admitted is not None
    assert next_admitted.suppressed_count == 1
    admitted = budget.admit(_event(), now=900)
    assert admitted is not None
    assert admitted.suppressed_count == 0


def test_budget_does_not_split_global_cooldown_by_resource():
    budget = AlertBudget(cooldown_seconds=900, hourly_cap=12)
    canonical = HealthEvent(
        rule="C4.graphiti_parked",
        category="C",
        title="test",
        reason="test",
        resource="graphiti_canonical",
    )
    search = HealthEvent(
        rule="C4.graphiti_parked",
        category="C",
        title="test",
        reason="test",
        resource="graphiti_search",
    )

    assert budget.admit(canonical, now=0) is not None
    assert budget.admit(search, now=1) is None


def test_budget_applies_global_hourly_cap():
    budget = AlertBudget(cooldown_seconds=0, hourly_cap=2)
    assert budget.admit(_event(rule="C1", session="1"), now=0) is not None
    assert budget.admit(_event(rule="C2", session="2"), now=1) is not None
    assert budget.admit(_event(rule="C3", session="3"), now=2) is None
    admitted = budget.admit(_event(rule="C3", session="3"), now=3600)
    assert admitted is not None
    assert admitted.suppressed_count == 1
