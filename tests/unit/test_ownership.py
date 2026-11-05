from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "plugins" / "filter"))
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "plugins" / "modules"))

from incus_compose import (  # noqa: E402
    incus_compose_ownership_snapshot,
    incus_compose_prune_plan,
    incus_compose_system_instance_matches,
)
from ownership import update_inventory, validate_snapshot  # noqa: E402


def manifest(name: str = "web") -> dict:
    return {
        "schema": "cloin.incus-compose/v1", "name": name,
        "placement": {"member": "node1"},
        "services": {
            "db": {"type": "container", "image": "images:debian/13",
                   "environment": {"EXAMPLE_SETTING": "fixture-marker"}},
            "api": {"type": "oci", "image": "docker:example/api",
                    "depends_on": ["db"]},
        },
        "networks": {"private": {}, "shared": {"name": "shared", "project": "default"}},
        "volumes": {
            "data": {"pool": "local", "retain": False},
            "archive": {"pool": "ceph", "retain": True},
        },
    }


def snapshot(name: str = "web") -> dict:
    return incus_compose_ownership_snapshot(
        manifest(name), {"local": "zfs", "ceph": "ceph"}, True
    )


def test_snapshot_contains_removal_identifiers_without_secrets():
    result = snapshot()
    assert result["services"] == [{"name": "db"}, {"name": "api"}]
    assert result["networks"] == [
        {"name": "web-private", "project": "default"},
        {"name": "shared", "project": "default"},
    ]
    assert result["volumes"] == [
        {"name": "data", "pool": "local", "target": "node1", "retain": False},
        {"name": "archive", "pool": "ceph", "target": "", "retain": True},
    ]
    assert "fixture-marker" not in json.dumps(result)
    assert "environment" not in json.dumps(result)
    validate_snapshot(result)


def test_reserved_live_ownership_marker_cannot_be_set_by_a_manifest():
    definition = manifest()
    definition["networks"]["private"]["config"] = {
        "user.cloin.incus_compose.owner": "forged"
    }
    with pytest.raises(AnsibleFilterError, match="reserved config key"):
        incus_compose_ownership_snapshot(
            definition, {"local": "zfs", "ceph": "ceph"}, True
        )


def test_boolish_manifest_fields_keep_existing_ansible_semantics():
    definition = manifest()
    definition["volumes"]["data"]["retain"] = "false"
    definition["networks"]["private"]["external"] = "false"
    definition["networks"]["private"]["config"] = {"ipv4.address": "auto"}
    result = incus_compose_ownership_snapshot(
        definition, {"local": "zfs", "ceph": "ceph"}, True
    )
    assert result["volumes"][0]["retain"] is False
    assert result["networks"][0]["name"] == "web-private"


def test_inventory_transitions_preserve_token_and_retired_state():
    data = {"version": 1, "records": {}}
    data, changed, pending = update_inventory(
        data, remote="lab", action="ensure", name="web", snapshot=snapshot()
    )
    assert changed and pending["status"] == "pending"
    token = pending["token"]
    data, changed, same = update_inventory(
        data, remote="lab", action="ensure", name="web", snapshot=snapshot()
    )
    assert not changed and same["token"] == token
    data, changed, active = update_inventory(
        data, remote="lab", action="publish", name="web",
        snapshot=snapshot(), token=token,
    )
    assert changed and active["status"] == "active"
    data, changed, active = update_inventory(
        data, remote="lab", action="publish", name="web",
        snapshot=snapshot(), token=token,
    )
    assert not changed
    data, changed, retired = update_inventory(
        data, remote="lab", action="retire", name="web", token=token,
        retained_volumes=["data", "archive"], retained_project=True,
    )
    assert changed and retired["status"] == "retired"
    data, changed, _ = update_inventory(
        data, remote="lab", action="remove", name="web", token=token
    )
    assert changed and data["records"] == {}


def test_inventory_rejects_ambiguous_ownership_and_extra_data():
    data = {"version": 1, "records": {}}
    data, _, record = update_inventory(
        data, remote="lab", action="ensure", name="web", snapshot=snapshot()
    )
    other = snapshot("other")
    other["project"] = "web"
    with pytest.raises(ValueError, match="already belongs"):
        update_inventory(data, remote="lab", action="ensure", name="other",
                         snapshot=other)
    with pytest.raises(ValueError, match="token mismatch"):
        update_inventory(data, remote="lab", action="retire", name="web",
                         token="wrong")
    polluted = copy.deepcopy(snapshot())
    polluted["services"][0]["environment"] = {"EXAMPLE_SETTING": "fixture-marker"}
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_snapshot(polluted)
    assert record["token"] != "wrong"


def test_publish_keeps_previously_owned_identifiers_for_later_prune():
    first = snapshot()
    data, _, pending = update_inventory(
        {"version": 1, "records": {}}, remote="lab", action="ensure",
        name="web", snapshot=first,
    )
    token = pending["token"]
    data, _, _ = update_inventory(
        data, remote="lab", action="publish", name="web",
        snapshot=first, token=token,
    )
    second = copy.deepcopy(first)
    second["services"] = [{"name": "api"}]
    second["volumes"] = []
    data, _, active = update_inventory(
        data, remote="lab", action="publish", name="web",
        snapshot=second, token=token,
    )
    assert {item["name"] for item in active["services"]} == {"db", "api"}
    assert {item["name"] for item in active["volumes"]} == {"data", "archive"}


def test_prune_selects_only_missing_active_applications():
    desired = [manifest("web")]
    old = {**snapshot("old"), "status": "active", "token": "old-token"}
    old["networks"] = [{"name": "old-private", "project": "default"}]
    retired = {**snapshot("gone"), "status": "retired", "token": "gone-token"}
    assert incus_compose_prune_plan(desired, [old, retired]) == [old]


def test_prune_refuses_empty_partial_and_shared_networks():
    old = {**snapshot("old"), "status": "active", "token": "old-token"}
    with pytest.raises(AnsibleFilterError, match="nonempty"):
        incus_compose_prune_plan([], [old])
    desired = manifest("web")
    desired["networks"]["shared"] = {"external": True, "name": "shared"}
    with pytest.raises(AnsibleFilterError, match="retiring network"):
        incus_compose_prune_plan([desired], [old])
    same_project = {**snapshot("other"), "project": "old",
                    "status": "active", "token": "other-token"}
    with pytest.raises(AnsibleFilterError, match="shared project"):
        incus_compose_prune_plan([manifest("web")], [old, same_project])
    pending = {**snapshot("partial"), "status": "pending", "token": "pending-token"}
    with pytest.raises(AnsibleFilterError, match="incomplete application"):
        incus_compose_prune_plan([manifest("web")], [pending])


def test_system_instance_match_ignores_ownership_marker_but_detects_drift():
    existing = {
        "description": "managed", "status": "Running", "profiles": [],
        "config": {
            "boot.autostart": "true", "environment.TOKEN": "value",
            "user.cloin.incus_compose.owner": "opaque",
            "volatile.uuid": "generated",
        },
        "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}},
    }
    config = {
        "boot.autostart": True,
        "environment_variables": [{"name": "TOKEN", "value": "value"}],
    }
    devices = [{"name": "root", "type": "disk", "path": "/", "pool": "default"}]
    assert incus_compose_system_instance_matches(
        existing, "managed", config, devices, [], "started"
    )
    existing["config"]["environment.TOKEN"] = "drift"
    assert not incus_compose_system_instance_matches(
        existing, "managed", config, devices, [], "started"
    )
