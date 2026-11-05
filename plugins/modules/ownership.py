#!/usr/bin/python
"""Persist nonsecret Incus Compose ownership on the Ansible execution host."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: ownership
short_description: Manage the Incus Compose ownership inventory
description:
  - Stores only resource identifiers and an opaque ownership token.
  - The inventory is local to the Ansible execution host and is updated atomically.
options:
  path:
    description: Host-persistent inventory file path.
    type: path
    required: true
  remote:
    description: Incus CLI remote used as the inventory namespace.
    type: str
    required: true
  action:
    description: Inventory operation to perform.
    type: str
    required: true
    choices: [read, ensure, publish, retire, remove]
  name:
    description: Application name for record operations.
    type: str
  snapshot:
    description: Allowlisted nonsecret resource identifiers.
    type: dict
  token:
    description: Existing ownership token required for mutations.
    type: str
  retained_volumes:
    description: Volume names retained after retirement.
    type: list
    elements: str
    default: []
  retained_project:
    description: Whether the project remains after retirement.
    type: bool
    default: false
"""

EXAMPLES = r"""
- name: Read ownership records
  cloin.incus_compose.ownership:
    path: ~/.local/state/cloin-incus-compose/ownership.json
    remote: lab
    action: read
"""

RETURN = r"""
records:
  description: Ownership records for the selected remote.
  type: list
  returned: always
record:
  description: The selected record, when a name is supplied.
  type: dict
  returned: when name is supplied
"""

import fcntl
import json
import os
import re
import tempfile
import uuid

from ansible.module_utils.basic import AnsibleModule


def validate_snapshot(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != {
        "name", "project", "services", "networks", "volumes"
    }:
        raise ValueError("snapshot must contain only ownership identifiers")
    for field in ("name", "project"):
        if not isinstance(value[field], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", value[field]
        ):
            raise ValueError(f"invalid snapshot {field}")
    fields = {
        "services": {"name"},
        "networks": {"name", "project"},
        "volumes": {"name", "pool", "target", "retain"},
    }
    for kind, allowed in fields.items():
        if not isinstance(value[kind], list):
            raise ValueError(f"snapshot {kind} must be a list")
        for item in value[kind]:
            if not isinstance(item, dict) or set(item) != allowed:
                raise ValueError(f"snapshot {kind} contains unsupported fields")
            for key, item_value in item.items():
                if key == "retain":
                    if not isinstance(item_value, bool):
                        raise ValueError("volume retain must be boolean")
                elif not isinstance(item_value, str):
                    raise ValueError(f"snapshot {kind}.{key} must be a string")


def validate_record(value: dict) -> None:
    fields = {"name", "project", "services", "networks", "volumes",
              "remote", "token", "status", "retained_volumes",
              "retained_project"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid ownership record fields")
    validate_snapshot({key: value[key] for key in
                       ("name", "project", "services", "networks", "volumes")})
    if not isinstance(value["remote"], str) or not value["remote"]:
        raise ValueError("invalid ownership remote")
    if not isinstance(value["token"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", value["token"]
    ):
        raise ValueError("invalid ownership token")
    if value["status"] not in {"pending", "active", "retired"}:
        raise ValueError("invalid ownership status")
    if (not isinstance(value["retained_volumes"], list) or
            any(not isinstance(item, str) for item in value["retained_volumes"])):
        raise ValueError("invalid retained volumes")
    if not isinstance(value["retained_project"], bool):
        raise ValueError("invalid retained project status")


def update_inventory(data: dict, *, remote: str, action: str, name: str = "",
                     snapshot: dict | None = None, token: str = "",
                     retained_volumes: list | None = None,
                     retained_project: bool = False) -> tuple[dict, bool, dict]:
    """Apply one inventory operation without accepting arbitrary manifest data."""
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        raise ValueError("unsupported ownership inventory format")
    records = data.setdefault("records", {})
    if not isinstance(records, dict):
        raise ValueError("invalid ownership inventory")
    for stored_key, stored_record in records.items():
        validate_record(stored_record)
        if stored_key != f"{stored_record['remote']}/{stored_record['name']}":
            raise ValueError("ownership record key mismatch")
    key = f"{remote}/{name}"
    old = records.get(key)
    if action == "read":
        return data, False, old or {}
    if action in {"ensure", "publish"}:
        if snapshot is None:
            raise ValueError("snapshot is required")
        validate_snapshot(snapshot)
        if snapshot["name"] != name:
            raise ValueError("snapshot name does not match inventory name")
        if snapshot["project"] == "default":
            raise ValueError("the shared default project cannot be application-owned")
    if action == "ensure":
        if old:
            if old["project"] != snapshot["project"]:
                raise ValueError("application project changed; retire it first")
            return data, False, old
        for existing in records.values():
            if (existing.get("remote") == remote and
                    existing.get("project") == snapshot["project"] and
                    existing.get("name") != name):
                raise ValueError("project already belongs to another application")
        record = {**snapshot, "remote": remote, "token": uuid.uuid4().hex,
                  "status": "pending", "retained_volumes": [],
                  "retained_project": False}
        records[key] = record
        return data, True, record
    if not old:
        raise ValueError(f"no ownership record for application {name}")
    if old.get("token") != token:
        raise ValueError("ownership token mismatch")
    if action == "publish":
        owned = dict(snapshot)
        if old.get("status") in {"active", "retired"}:
            identities = {
                "services": lambda item: item["name"],
                "networks": lambda item: (item["project"], item["name"]),
                "volumes": lambda item: (item["pool"], item["target"], item["name"]),
            }
            for kind, identity in identities.items():
                combined = {identity(item): item for item in old[kind]}
                combined.update({identity(item): item for item in snapshot[kind]})
                owned[kind] = list(combined.values())
        record = {**owned, "remote": remote, "token": token,
                  "status": "active", "retained_volumes": [],
                  "retained_project": False}
        changed = record != old
        records[key] = record
        return data, changed, record
    if action == "retire":
        record = {**old, "status": "retired",
                  "retained_volumes": retained_volumes or [],
                  "retained_project": retained_project}
        changed = record != old
        records[key] = record
        return data, changed, record
    if action == "remove":
        del records[key]
        return data, True, {}
    raise ValueError("unknown inventory action")


def main() -> None:
    module = AnsibleModule(argument_spec={
        "path": {"type": "path", "required": True},
        "remote": {"type": "str", "required": True},
        "action": {"type": "str", "required": True,
                   "choices": ["read", "ensure", "publish", "retire", "remove"]},
        "name": {"type": "str", "default": ""},
        "snapshot": {"type": "dict"},
        "token": {"type": "str", "default": ""},
        "retained_volumes": {"type": "list", "elements": "str", "default": []},
        "retained_project": {"type": "bool", "default": False},
    }, supports_check_mode=True)
    args = module.params
    path = os.path.abspath(os.path.expanduser(args["path"]))
    if not args["remote"] or (args["action"] != "read" and not args["name"]):
        module.fail_json(msg="remote and application name are required")
    if args["action"] == "read" and not os.path.exists(path):
        module.exit_json(changed=False, records=[], record={})
    try:
        if module.check_mode and not os.path.exists(path):
            updated, changed, record = update_inventory(
                {"version": 1, "records": {}}, remote=args["remote"],
                action=args["action"], name=args["name"],
                snapshot=args["snapshot"], token=args["token"],
                retained_volumes=args["retained_volumes"],
                retained_project=args["retained_project"],
            )
            module.exit_json(changed=changed, records=list(updated["records"].values()),
                             record=record)
        if not module.check_mode and args["action"] != "read":
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".lock", "a+", encoding="utf-8") as lock:
            os.chmod(path + ".lock", 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as stream:
                    data = json.load(stream)
            else:
                data = {"version": 1, "records": {}}
            updated, changed, record = update_inventory(
                data, remote=args["remote"], action=args["action"],
                name=args["name"], snapshot=args["snapshot"],
                token=args["token"], retained_volumes=args["retained_volumes"],
                retained_project=args["retained_project"],
            )
            if changed and not module.check_mode:
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".ownership-", dir=os.path.dirname(path)
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        json.dump(updated, stream, sort_keys=True, separators=(",", ":"))
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            selected = [item for item in updated["records"].values()
                        if item.get("remote") == args["remote"]]
            module.exit_json(changed=changed, records=selected, record=record)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        module.fail_json(msg=str(error))


if __name__ == "__main__":
    main()
