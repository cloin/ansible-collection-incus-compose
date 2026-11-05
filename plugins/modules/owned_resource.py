#!/usr/bin/python
"""Reconcile an Incus project, network, or volume with ownership metadata."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: owned_resource
short_description: Reconcile a project, network, or volume with ownership metadata
description:
  - Uses the damex.incus resource engine with an unrestricted Incus config mapping.
  - The caller adds the collection-owned user config key to that mapping.
extends_documentation_fragment:
  - damex.incus.common
options:
  kind:
    description: Incus resource kind.
    type: str
    required: true
    choices: [project, network, volume]
  name:
    description: Physical Incus resource name.
    type: str
    required: true
  state:
    description: Desired lifecycle state.
    type: str
    choices: [present, absent]
    default: present
  project:
    description: Project containing a network or volume.
    type: str
  target:
    description: Cluster member for a local volume.
    type: str
  pool:
    description: Storage pool for a custom volume.
    type: str
  type:
    description: Network type when creating a network.
    type: str
  content_type:
    description: Content type when creating a custom volume.
    type: str
  description:
    description: Human-readable resource description.
    type: str
  config:
    description: Incus config including the collection ownership marker.
    type: dict
"""

EXAMPLES = r"""
- name: Ensure a tagged network
  cloin.incus_compose.owned_resource:
    kind: network
    name: app-private
    project: default
    type: bridge
    config:
      user.cloin.incus_compose.owner: token
"""

RETURN = r"""
extends_documentation_fragment:
  - damex.incus.common.write_return
"""

from urllib.parse import quote

from ansible_collections.damex.incus.plugins.module_utils.incus import (
    INCUS_COMMON_ARGUMENT_SPEC,
    IncusResourceOptions,
    incus_build_desired,
    incus_create_write_module,
    incus_ensure_resource,
    incus_run_write_module,
)
from ansible_collections.damex.incus.plugins.module_utils.incus_target import (
    incus_is_network_node_specific,
)


def main() -> None:
    spec = {
        **INCUS_COMMON_ARGUMENT_SPEC,
        "kind": {"type": "str", "required": True,
                 "choices": ["project", "network", "volume"]},
        "project": {"type": "str"},
        "target": {"type": "str"},
        "pool": {"type": "str"},
        "type": {"type": "str"},
        "content_type": {"type": "str", "default": "filesystem"},
        "description": {"type": "str", "default": ""},
        "config": {"type": "dict", "default": {}},
    }
    module = incus_create_write_module(spec)
    kind = module.params["kind"]
    if kind == "volume" and not module.params["pool"]:
        module.fail_json(msg="pool is required for a volume")
    if kind == "network" and not module.params["type"]:
        module.fail_json(msg="type is required for a network")
    desired = incus_build_desired(
        module, config_lists={"bgp_peers": "bgp.peers", "tunnels": "tunnel"}
        if kind == "network" else None,
    )
    if kind == "project":
        resource = "projects"
        options = IncusResourceOptions()
    elif kind == "network":
        resource = "networks"
        options = IncusResourceOptions(
            create_only_params=["type"],
            is_node_specific=incus_is_network_node_specific,
        )
    else:
        resource = "storage-pools/" + quote(module.params["pool"], safe="") + "/volumes/custom"
        options = IncusResourceOptions(create_only_params=["content_type"])
    incus_run_write_module(
        module, lambda: incus_ensure_resource(module, resource, desired, options)
    )


if __name__ == "__main__":
    main()
