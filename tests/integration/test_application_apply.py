"""Exercise the normal application role against a small local Incus fixture."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = pathlib.Path(__file__).parents[2]
OWNER = "user.cloin.incus_compose.owner"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                    reason="ansible-playbook is required")
def test_enroll_existing_resources_then_apply_idempotently(tmp_path):
    collections = tmp_path / "collections" / "ansible_collections"
    own = collections / "cloin" / "incus_compose"
    own.mkdir(parents=True)
    (own / "roles").symlink_to(ROOT / "roles", target_is_directory=True)
    (own / "galaxy.yml").symlink_to(ROOT / "galaxy.yml")
    (own / "plugins").mkdir()
    (own / "plugins" / "filter").symlink_to(
        ROOT / "plugins" / "filter", target_is_directory=True
    )
    own_modules = own / "plugins" / "modules"
    own_modules.mkdir()
    (own_modules / "ownership.py").symlink_to(
        ROOT / "plugins" / "modules" / "ownership.py"
    )

    module_source = '''
import json
import os
from ansible.module_utils.basic import AnsibleModule

kind = '__KIND__'
spec = {
    'name': {'type': 'str'}, 'project': {'type': 'str'},
    'url': {'type': 'str'}, 'client_cert_path': {'type': 'str'},
    'client_key_path': {'type': 'str'}, 'server_cert_path': {'type': 'str'},
    'validate_certs': {'type': 'bool'}, 'pool': {'type': 'str'},
    'target': {'type': 'str'}, 'state': {'type': 'str'},
    'description': {'type': 'str'}, 'config': {'type': 'dict'},
    'type': {'type': 'str'}, 'content_type': {'type': 'str'},
    'source': {'type': 'str'}, 'source_server': {'type': 'str'},
    'source_protocol': {'type': 'str'}, 'profiles': {'type': 'list'},
    'devices': {'type': 'list'}, 'ephemeral': {'type': 'bool'},
    'kind': {'type': 'str'},
}
module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
path = os.environ['FAKE_INCUS_STATE']
with open(path) as stream:
    data = json.load(stream)
args = module.params
name = args['name']
if kind == 'incus_cluster_info':
    module.exit_json(changed=False, cluster={'enabled': False})
if kind == 'incus_cluster_member_info':
    module.exit_json(changed=False, cluster_members=[])
if kind == 'incus_storage_info':
    module.exit_json(changed=False, storage_pools=[{'name': 'default', 'driver': 'dir'}])
for info_kind, bucket, result in (
    ('incus_project_info', 'projects', 'projects'),
    ('incus_network_info', 'networks', 'networks'),
    ('incus_instance_info', 'instances', 'instances'),
    ('targeted_volume_info', 'volumes', 'storage_volumes'),
):
    if kind == info_kind:
        value = data[bucket].get(name)
        module.exit_json(changed=False, **{result: [value] if value else []})
if kind == 'owned_resource':
    bucket = {'project': 'projects', 'network': 'networks',
              'volume': 'volumes'}[args['kind']]
    desired = {'name': name, 'description': args['description'],
               'config': args['config']}
    if bucket == 'networks':
        desired['type'] = args['type']
    if bucket == 'volumes':
        desired['content_type'] = args['content_type']
    changed = data[bucket].get(name) != desired
    if changed:
        data[bucket][name] = desired
        data['events'].append('apply ' + bucket + '/' + name)
elif kind == 'incus_instance':
    config = {key: str(value).lower() if isinstance(value, bool) else str(value)
              for key, value in args['config'].items()}
    desired = {'name': name, 'description': args['description'],
               'config': config, 'devices': {},
               'profiles': args['profiles'],
               'status': 'Running' if args['state'] == 'started' else 'Stopped'}
    existing = data['instances'].get(name)
    if existing and 'user.cloin.incus_compose.owner' in existing['config']:
        desired['config']['user.cloin.incus_compose.owner'] = (
            existing['config']['user.cloin.incus_compose.owner'])
    changed = existing != desired
    if changed:
        data['instances'][name] = desired
        data['events'].append('apply instances/' + name)
else:
    module.fail_json(msg='unsupported test module ' + kind)
if changed and not module.check_mode:
    with open(path, 'w') as stream:
        json.dump(data, stream)
module.exit_json(changed=changed)
'''
    (own_modules / "owned_resource.py").write_text(
        module_source.replace("__KIND__", "owned_resource")
    )
    (own_modules / "targeted_volume_info.py").write_text(
        module_source.replace("__KIND__", "targeted_volume_info")
    )
    damex = collections / "damex" / "incus"
    (damex / "plugins" / "modules").mkdir(parents=True)
    (damex / "galaxy.yml").write_text(
        "namespace: damex\nname: incus\nversion: 1.11.8\n"
    )
    for name in ("incus_cluster_info", "incus_cluster_member_info",
                 "incus_storage_info", "incus_project_info", "incus_network",
                 "incus_network_info", "incus_instance_info", "incus_instance"):
        (damex / "plugins" / "modules" / f"{name}.py").write_text(
            module_source.replace("__KIND__", name)
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    incus = bin_dir / "incus"
    incus.write_text('''#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
if argv[:2] == ['info', 'fake:']:
    sys.exit(0)
path = os.environ['FAKE_INCUS_STATE']
with open(path) as stream:
    data = json.load(stream)
if argv[0] == 'project':
    bucket, action, name, marker = 'projects', argv[1], argv[2].split(':', 1)[1], argv[3]
elif argv[0] == 'network':
    bucket, action, name, marker = 'networks', argv[1], argv[2].split(':', 1)[1], argv[3]
elif argv[0] == 'config':
    bucket, action, name, marker = 'instances', argv[1], argv[2].split(':', 1)[1], argv[3]
elif argv[:2] == ['storage', 'volume']:
    bucket, action, name, marker = 'volumes', argv[2], argv[4], argv[5]
else:
    raise AssertionError(argv)
assert marker.startswith('user.cloin.incus_compose.owner')
value = data[bucket][name]['config'].get('user.cloin.incus_compose.owner', '')
if action == 'get':
    print(value)
elif action == 'set':
    data[bucket][name]['config']['user.cloin.incus_compose.owner'] = marker.split('=', 1)[1]
    data['events'].append('mark ' + bucket + '/' + name)
    with open(path, 'w') as stream:
        json.dump(data, stream)
else:
    raise AssertionError(argv)
''')
    incus.chmod(0o755)

    state = tmp_path / "state.json"
    initial = {
        "projects": {"app": {"name": "app", "description": "old", "config": {}}},
        "networks": {"app-net": {"name": "app-net", "description": "old",
                                  "type": "bridge", "config": {}}},
        "volumes": {"data": {"name": "data", "description": "old",
                             "content_type": "filesystem", "config": {}}},
        "instances": {}, "events": [],
    }
    state.write_text(json.dumps(initial))
    inventory = tmp_path / "ownership.json"
    play = tmp_path / "apply.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {
            "ansible_python_interpreter": sys.executable,
            "incus_compose_connection": {
                "remote": "fake", "url": "https://example.invalid",
                "client_cert_path": "/tmp/fixture.crt",
                "client_key_path": "/tmp/fixture.key",
            },
            "incus_compose_ownership_path": str(inventory),
            "incus_compose_enroll_existing": "{{ lookup('env', 'FAKE_ENROLL') | bool }}",
            "incus_compose_application": {
                "schema": "cloin.incus-compose/v1", "name": "app",
                "services": {"web": {"type": "container", "image": "images:debian/13",
                                     "profiles": ["default"]}},
                "networks": {"net": {}}, "volumes": {"data": {}},
            },
        },
        "tasks": [{"ansible.builtin.include_role": {
            "name": "cloin.incus_compose.application"}}],
    }]))
    environment = dict(os.environ)
    environment["ANSIBLE_COLLECTIONS_PATH"] = str(tmp_path / "collections")
    environment["FAKE_INCUS_STATE"] = str(state)
    environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]

    def apply(enroll):
        environment["FAKE_ENROLL"] = "true" if enroll else "false"
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
            env=environment, capture_output=True, text=True, check=False,
        )

    refused = apply(False)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert json.loads(state.read_text()) == initial
    assert not inventory.exists()

    first = apply(True)
    assert first.returncode == 0, first.stdout + first.stderr
    current = json.loads(state.read_text())
    records = json.loads(inventory.read_text())["records"]
    record = records["fake/app"]
    assert record["status"] == "active"
    assert record["project"] == "app"
    assert record["services"] == [{"name": "web"}]
    assert record["networks"] == [{"name": "app-net", "project": "default"}]
    assert record["volumes"][0]["name"] == "data"
    token = record["token"]
    assert len(token) == 32
    for bucket, name in (("projects", "app"), ("networks", "app-net"),
                         ("volumes", "data"), ("instances", "web")):
        assert current[bucket][name]["config"][OWNER] == token
    assert len(current["events"]) == 5

    second = apply(False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "changed=0" in second.stdout
    assert json.loads(state.read_text()) == current
    assert json.loads(inventory.read_text())["records"] == records
