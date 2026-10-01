"""The agent layer, tested against a fake Agents API session (no network, no OpenAI key)."""

import json
from types import SimpleNamespace as NS

import pytest
from botocore.exceptions import ClientError

from grimreaper import watch
from grimreaper.agent import report_schema
from grimreaper.models import Resource
from grimreaper.runtime import AgentRunError, Tool, run_session


class FakeSessions:
    """Scripted session: the agent calls tools a few times, then its turn finishes."""

    def __init__(self, calls_per_round, final_text, root_status="completed"):
        self.rounds = list(calls_per_round)
        self.final_text = final_text
        self.root_status = root_status
        self.submitted = []
        self.created = None
        self.deleted = False
        self.events = NS(create=self._submit)
        self.turns = NS(list=self._turns)
        self.items = NS(list=self._items)

    def create(self, **kwargs):
        self.created = kwargs
        return NS(id="sess_1")

    def retrieve(self, sid):
        if self.rounds:
            return NS(status="requires_action", required_actions=self.rounds[0], error=None)
        return NS(status="idle", required_actions=[], error=None)

    def _submit(self, sid, events):
        self.submitted.extend(events)
        if events[0]["type"] == "agent.session.input.tool_result":
            self.rounds.pop(0)

    def _turns(self, sid, order):
        status = "in_progress" if self.rounds else self.root_status
        err = NS(code="usage_limit_exceeded", message="out of credits") if status == "failed" else None
        return [NS(id="turn_root", subagent_id=None, status=status, error=err)]

    def _items(self, sid, order):
        return [
            NS(type="message", role="assistant", turn_id="turn_root", phase="commentary", content=[NS(text="working on it")]),
            NS(type="message", role="assistant", turn_id="turn_root", phase="final_answer", content=[NS(text=self.final_text)]),
        ]

    def delete(self, sid):
        self.deleted = True


def fake_client(sessions):
    return NS(beta=NS(agents=NS(sessions=sessions)))


def call(name, args, call_id, turn="turn_root"):
    return NS(type="function_call", name=name, arguments=json.dumps(args), call_id=call_id, turn_id=turn)


def test_tools_run_locally_and_results_go_back_to_the_session():
    seen = []
    tools = [Tool("echo", "echo", {"type": "object"}, lambda a: seen.append(a) or {"you_said": a["x"]})]
    # Round 1: two calls requested at once. Round 2: one more.
    sessions = FakeSessions(
        [[call("echo", {"x": 1}, "c1"), call("echo", {"x": 2}, "c2")], [call("echo", {"x": 3}, "c3")]],
        final_text="done",
    )

    out = run_session(fake_client(sessions), instructions="i", task="t", tools=tools, poll_s=0)

    assert out == "done"  # the final answer, not the commentary
    assert sorted(a["x"] for a in seen) == [1, 2, 3]
    assert [e["call_id"] for e in sessions.submitted] == ["c1", "c2", "c3"]
    assert all(e["success"] for e in sessions.submitted)
    assert "multi_agent" not in sessions.created["agent"]
    assert sessions.created["environment"] == {"type": "none"}  # no sandbox: AWS creds stay local
    assert sessions.deleted


def test_tool_errors_are_reported_without_leaking_internals():
    def denied(_):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "GetCostAndUsage")

    def crash(_):
        raise RuntimeError("secret=abc123")

    tools = [Tool("denied", "", {}, denied), Tool("crash", "", {}, crash)]
    sessions = FakeSessions([[call("denied", {}, "c1"), call("crash", {}, "c2"), call("nope", {}, "c3")]], "ok")

    run_session(fake_client(sessions), instructions="", task="", tools=tools, poll_s=0)

    errors = {e["call_id"]: e["error"] for e in sessions.submitted}
    assert errors["c1"] == "AWS AccessDenied: not allowed"
    assert "secret" not in errors["c2"]
    assert "unknown tool" in errors["c3"]


def test_failed_turn_raises_with_reason():
    sessions = FakeSessions([], "", root_status="failed")
    with pytest.raises(AgentRunError, match="usage_limit_exceeded"):
        run_session(fake_client(sessions), instructions="", task="", tools=[], poll_s=0)
    assert sessions.deleted


def test_report_schema_is_closed_for_structured_output():
    schema = report_schema()
    verdict = schema["$defs"]["Verdict"]
    assert schema["additionalProperties"] is False and verdict["additionalProperties"] is False
    assert set(verdict["required"]) == set(verdict["properties"])


def _inv(*resources):
    return {r.key: r for r in resources}


def test_watch_stays_quiet_when_nothing_changed():
    vol = Resource("ebs_volume", "vol-1", "us-east-1", monthly_cost=1.6)
    costs = {"recent_usd_per_day": 0.05, "previous_usd_per_day": 0.05, "usage_types": [
        {"usage_type": "EBS:VolumeUsage", "service": "EC2", "status": "steady",
         "recent_usd_per_day": 0.05, "previous_usd_per_day": 0.05},
    ]}
    previous = {"checked_at": "2026-09-30", "resources": {vol.key: vol.to_dict()}}

    changes = watch.diff(previous, _inv(vol), costs)

    assert watch.is_quiet(changes)


def test_watch_wakes_up_for_new_resources_and_new_charges():
    old = Resource("ebs_volume", "vol-1", "us-east-1")
    nat = Resource("nat_gateway", "nat-1", "us-east-1", monthly_cost=32.85)
    costs = {"recent_usd_per_day": 1.2, "previous_usd_per_day": 0.1, "usage_types": [
        {"usage_type": "NatGateway-Hours", "service": "EC2", "status": "new",
         "recent_usd_per_day": 1.08, "previous_usd_per_day": 0.0},
        {"usage_type": "DNS-Queries", "service": "Route 53", "status": "new",  # pennies: ignored
         "recent_usd_per_day": 0.001, "previous_usd_per_day": 0.0},
    ]}
    previous = {"checked_at": "2026-09-30", "resources": {old.key: old.to_dict(), "s3_bucket:global:gone": {}}}

    changes = watch.diff(previous, _inv(old, nat), costs)

    assert not watch.is_quiet(changes)
    assert [r["key"] for r in changes["new_resources"]] == [nat.key]
    assert changes["removed_resources"] == ["s3_bucket:global:gone"]
    assert [c["usage_type"] for c in changes["new_or_rising_charges"]] == ["NatGateway-Hours"]


def test_first_watch_run_always_investigates():
    assert not watch.is_quiet(watch.diff(None, {}, {"recent_usd_per_day": 0, "previous_usd_per_day": 0, "usage_types": []}))


def test_cloudtrail_classifies_creates_and_deletes_and_skips_noise():
    from grimreaper.trail import _classify

    assert _classify("DeleteWebACL") == "deleted"
    assert _classify("DeregisterImage") == "deleted"
    assert _classify("AllocateAddress") == "created"
    assert _classify("PutLogEvents") is None
    assert _classify("AuthorizeSecurityGroupIngress") is None


def test_progress_counts_tool_usage():
    tools = [Tool("scan_inventory", "", {}, lambda a: {}), Tool("check_utilization", "", {}, lambda a: {})]
    sessions = FakeSessions([[call("scan_inventory", {}, "c1"), call("check_utilization", {}, "c2")],
                             [call("check_utilization", {}, "c3")]], "done")
    seen = []

    run_session(fake_client(sessions), instructions="", task="", tools=tools, poll_s=0, on_progress=seen.append)

    final = seen[-1]
    assert final.done and final.tool_calls == 3
    assert dict(final.tools_used) == {"scan_inventory": 1, "check_utilization": 2}


def test_failed_run_cancels_the_session_before_deleting_it():
    sessions = FakeSessions([], "", root_status="failed")
    with pytest.raises(AgentRunError):
        run_session(fake_client(sessions), instructions="", task="", tools=[], poll_s=0)
    assert sessions.submitted[-1] == {"type": "agent.session.input.cancel"}
    assert sessions.deleted
