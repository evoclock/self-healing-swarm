from pathlib import Path

import pytest

from runtime.config import MissingTenant, UnknownTool, load_agent, load_agents

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_unknown_tool_is_rejected():
    with pytest.raises(UnknownTool) as caught:
        load_agent(FIXTURES / "unknown_tool.yaml")
    assert caught.value.code == "unknown_tool"
    assert caught.value.tool == "shell.exec"


def test_missing_tenant_is_rejected():
    with pytest.raises(MissingTenant) as caught:
        load_agent(FIXTURES / "no_tenant.yaml")
    assert caught.value.code == "missing_tenant"


def test_four_tenants_each_have_two_agents():
    configs = load_agents(ROOT / "agents")
    by_tenant = {}
    for config in configs:
        by_tenant.setdefault(config.tenant_id, set()).add(config.id)
    assert set(by_tenant) == {"t-a", "t-b", "t-c", "t-d"}
    for tenant, ids in by_tenant.items():
        assert ids == {"daily-brief", "doc-analyst"}
    brief = next(c for c in configs if c.tenant_id == "t-a" and c.id == "daily-brief")
    assert brief.tools == ("mail.list", "mail.read", "memory.write", "draft.create")
    assert brief.models == ("primary", "local")
    assert brief.deadline == "07:30"
