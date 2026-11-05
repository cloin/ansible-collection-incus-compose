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


@pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                    reason="ansible-playbook is required")
@pytest.mark.parametrize(
    "automatic,retain,destroy_data,stop_fails,bad_network_marker",
    [
        (False, True, False, False, False),
        (True, False, False, False, False),
        (False, False, False, False, False),
        (True, True, True, False, False),
        (False, True, False, True, False),
        (True, True, False, False, True),
    ],
)
def test_running_services_stop_in_reverse_order_and_repeat_is_unchanged(
    tmp_path, automatic, retain, destroy_data, stop_fails, bad_network_marker
):
    collections = tmp_path / "collections" / "ansible_collections"
    own = collections / "cloin" / "incus_compose"
    own.mkdir(parents=True)
    (own / "roles").symlink_to(ROOT / "roles", target_is_directory=True)
    (own / "galaxy.yml").symlink_to(ROOT / "galaxy.yml")
    own_modules = own / "plugins" / "modules"
    own_modules.mkdir(parents=True)
    (own_modules / "ownership.py").symlink_to(ROOT / "plugins" / "modules" / "ownership.py")
    (own_modules / "targeted_volume_info.py").write_text("""
import json
import os
from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec={
    'name': {'type': 'str'}, 'pool': {'type': 'str'},
    'project': {'type': 'str'}, 'target': {'type': 'str'},
    'url': {'type': 'str'}, 'client_cert_path': {'type': 'str'},
    'client_key_path': {'type': 'str'}, 'server_cert_path': {'type': 'str'},
    'validate_certs': {'type': 'bool'},
})
with open(os.environ['FAKE_INCUS_STATE']) as stream:
    data = json.load(stream)
value = data['volumes'].get(module.params['name'])
module.exit_json(changed=False, storage_volumes=[value] if value else [])
""")
    modules = collections / "damex" / "incus" / "plugins" / "modules"
    modules.mkdir(parents=True)
    (collections / "damex" / "incus" / "galaxy.yml").write_text(
        "namespace: damex\nname: incus\nversion: 1.11.8\n"
    )
    module_source = """
import json
import os
from ansible.module_utils.basic import AnsibleModule

module = AnsibleModule(argument_spec={
    'name': {'type': 'str'}, 'project': {'type': 'str'},
    'state': {'type': 'str'}, 'url': {'type': 'str'},
    'pool': {'type': 'str'}, 'target': {'type': 'str'},
    'client_cert_path': {'type': 'str'}, 'client_key_path': {'type': 'str'},
    'server_cert_path': {'type': 'str'}, 'validate_certs': {'type': 'bool'},
}, supports_check_mode=True)
path = os.environ['FAKE_INCUS_STATE']
with open(path) as stream:
    data = json.load(stream)
name = module.params['name']
kind = '__MODULE_KIND__'
if kind == 'incus_instance_info':
    value = data['instances'].get(name)
    module.exit_json(changed=False, instances=[value] if value else [])
if kind == 'incus_project_info':
    value = data['projects'].get(name)
    module.exit_json(changed=False, projects=[value] if value else [])
if kind == 'incus_network_info':
    value = data['networks'].get(name)
    module.exit_json(changed=False, networks=[value] if value else [])
if kind == 'incus_instance':
    value = data['instances'].get(name)
    if value and value['status'] == 'Running':
        module.fail_json(msg='cannot delete a running instance')
    if value:
        del data['instances'][name]
        data['events'].append('delete ' + name)
        with open(path, 'w') as stream:
            json.dump(data, stream)
    module.exit_json(changed=bool(value))
if kind == 'incus_storage_volume':
    value = data['volumes'].pop(name, None)
    if value:
        data['events'].append('delete volume ' + name)
        with open(path, 'w') as stream:
            json.dump(data, stream)
    module.exit_json(changed=bool(value))
if kind == 'incus_project':
    value = data['projects'].get(name)
    if value:
        del data['projects'][name]
        data['events'].append('delete project ' + name)
        with open(path, 'w') as stream:
            json.dump(data, stream)
    module.exit_json(changed=bool(value))
module.fail_json(msg='unsupported test module')
"""
    for name in ("incus_instance_info", "incus_instance", "incus_project_info",
                 "incus_project", "incus_network_info", "incus_storage_volume"):
        (modules / f"{name}.py").write_text(
            module_source.replace("__MODULE_KIND__", name)
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    incus = bin_dir / "incus"
    incus.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "path = os.environ['FAKE_INCUS_STATE']\n"
        "with open(path) as stream: data = json.load(stream)\n"
        "assert sys.argv[1] == 'stop'\n"
        "assert '--timeout' in sys.argv and '--force' not in sys.argv\n"
        "if os.environ.get('FAKE_STOP_FAIL'): sys.exit(2)\n"
        "name = sys.argv[2].split(':', 1)[1]\n"
        "assert data['instances'][name]['status'] == 'Running'\n"
        "data['instances'][name]['status'] = 'Stopped'\n"
        "data['events'].append('stop ' + name)\n"
        "with open(path, 'w') as stream: json.dump(data, stream)\n"
    )
    incus.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "instances": {
            "db": {"name": "db", "status": "Running",
                   "config": {"user.cloin.incus_compose.owner": "expected"}},
            "api": {"name": "api", "status": "Running",
                    "config": {"user.cloin.incus_compose.owner": "expected"}},
        },
        "projects": {"app": {"name": "app",
                             "config": {"user.cloin.incus_compose.owner": "expected"}}},
        "networks": {"shared": {"name": "shared",
                                 "config": {"user.cloin.incus_compose.owner": "other"}}}
        if bad_network_marker else {},
        "volumes": {"data": {"name": "data", "config": {}}},
        "events": [],
    }))
    play = tmp_path / "retire.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {
            "ansible_python_interpreter": sys.executable,
            "incus_compose_connection": {
                "remote": "fake", "url": "https://example.invalid",
                "client_cert_path": "/tmp/fake.crt", "client_key_path": "/tmp/fake.key",
            },
            "incus_compose_retire_verify": bad_network_marker,
            "incus_compose_retire_automatic": automatic,
            "incus_compose_destroy_data": destroy_data,
            "incus_compose_retire_record": {
                "name": "app", "project": "app",
                "services": [{"name": "db"}, {"name": "api"}],
                "networks": [{"name": "shared", "project": "default"}]
                if bad_network_marker else [],
                "volumes": [{"name": "data", "pool": "default",
                             "target": "", "retain": retain}],
                **({"token": "expected"} if bad_network_marker else {}),
            },
        },
        "tasks": [
            {"ansible.builtin.include_role": {"name": "cloin.incus_compose.retire"}},
            {"ansible.builtin.assert": {"that": [
                "incus_compose_retirement_result.retained_volumes == " +
                ("[]" if destroy_data or (not automatic and not retain) else "['data']"),
                "incus_compose_retirement_result.retained_project == " +
                ("false" if destroy_data or (not automatic and not retain) else "true"),
            ]}},
        ],
    }]))
    environment = dict(os.environ)
    environment["ANSIBLE_COLLECTIONS_PATH"] = str(tmp_path / "collections")
    environment["FAKE_INCUS_STATE"] = str(state)
    environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]
    if stop_fails:
        environment["FAKE_STOP_FAIL"] = "1"
    expected_events = ["stop api", "delete api", "stop db", "delete db"]
    volume_retained = not destroy_data and (automatic or retain)
    if not volume_retained:
        expected_events += ["delete volume data", "delete project app"]
    for _ in range(2):
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
            env=environment, capture_output=True, text=True, check=False,
        )
        current = json.loads(state.read_text())
        if stop_fails or bad_network_marker:
            assert result.returncode != 0
            assert current["events"] == []
            assert set(current["instances"]) == {"db", "api"}
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert current["events"] == expected_events
        assert current["instances"] == {}
        assert ("app" in current["projects"]) == volume_retained
        assert ("data" in current["volumes"]) == volume_retained
