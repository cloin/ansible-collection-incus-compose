#!/usr/bin/python
"""Read one custom volume, including its cluster member placement."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: targeted_volume_info
short_description: Inspect a custom Incus volume on its recorded cluster member
description:
  - Reads a custom volume with the project and optional target recorded by Incus Compose.
extends_documentation_fragment:
  - damex.incus.common
  - damex.incus.common.project
options:
  pool:
    description: Storage pool containing the custom volume.
    type: str
    required: true
  name:
    description: Custom volume name.
    type: str
    required: true
  target:
    description: Cluster member for a local volume.
    type: str
"""

EXAMPLES = r"""
- name: Inspect a local volume
  cloin.incus_compose.targeted_volume_info:
    pool: local
    name: data
    project: app
    target: node1
"""

RETURN = r"""
storage_volumes:
  description: Zero or one matching custom volume.
  type: list
  returned: always
"""

from urllib.parse import quote

from ansible_collections.damex.incus.plugins.module_utils.incus import (
    incus_build_query,
    incus_create_info_module,
)
from ansible_collections.damex.incus.plugins.module_utils.incus_client import (
    IncusClientException,
    IncusNotFoundException,
    incus_create_client,
)


def main() -> None:
    module = incus_create_info_module({
        "pool": {"type": "str", "required": True},
        "name": {"type": "str", "required": True},
        "project": {"type": "str", "default": "default"},
        "target": {"type": "str"},
    })
    params = module.params
    path = (
        "/1.0/storage-pools/" + quote(params["pool"], safe="") +
        "/volumes/custom/" + quote(params["name"], safe="") +
        incus_build_query(params["project"], params["target"])
    )
    try:
        with incus_create_client(module) as client:
            try:
                metadata = client.get(path).get("metadata")
            except IncusNotFoundException:
                metadata = None
    except IncusClientException as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False, storage_volumes=[metadata] if metadata else [])


if __name__ == "__main__":
    main()
