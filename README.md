# cloin.incus_compose

`cloin.incus_compose` gives Ansible a Compose-like application model for
Incus. One manifest can own a project, managed or external networks, custom
volumes, OCI containers, system containers, and virtual machines. A catalog
role validates cross-application collisions, reconciles applications in
network-dependency order, and resolves HTTP routes from live Incus addresses.

This early collection is installed directly from Git. It is not published to
Ansible Galaxy.

## Install

Pin an exact Git tag in your project's `requirements.yml`:

```yaml
---
collections:
  - name: https://github.com/cloin/ansible-collection-incus-compose.git
    type: git
    version: v0.1.1
```

Then install it and its `damex.incus` dependency:

```console
ansible-galaxy collection install -r requirements.yml
```

The controller needs the `incus` CLI and an already configured remote. The
same client certificate is supplied to the Ansible modules:

```yaml
incus_compose_connection:
  remote: lab
  url: https://incus.example.net:8443
  client_cert_path: "{{ lookup('env', 'HOME') }}/.config/incus/client.crt"
  client_key_path: "{{ lookup('env', 'HOME') }}/.config/incus/client.key"
  validate_certs: true
```

## Reconcile one application

```yaml
---
- name: Apply Linkwarden
  hosts: incus_api
  gather_facts: false
  roles:
    - role: cloin.incus_compose.application
      incus_compose_application: "{{ linkwarden_definition }}"
```

## Reconcile a catalog

The catalog role loads every `*.yaml` file in stable order, validates the whole
set, ensures the owner of a shared network runs before its consumers, and
returns resolved routes as `incus_compose_catalog_result.routes`.

```yaml
---
- name: Apply all applications
  hosts: incus_api
  gather_facts: false
  roles:
    - role: cloin.incus_compose.catalog
      incus_compose_catalog_directory: "{{ playbook_dir }}/../apps"
```

Pass `incus_compose_catalog` as a list instead of a directory when definitions
are already loaded. Set exactly one input. Common role controls include
`incus_compose_default_storage_pool`, `incus_compose_update`,
`incus_compose_destroy_data`, and `incus_compose_resolve_routes`.

## Ownership and catalog pruning

Successful application reconciliation records a small, nonsecret inventory at
`~/.local/state/cloin-incus-compose/ownership.json` on the **Ansible execution
host**. Set `incus_compose_ownership_path` to a persistent absolute path when
that host or its home directory can change. Use one execution host per Incus
remote and keep the inventory file across deployments. The file contains only
application and resource identifiers, storage placement, retention settings,
and an opaque ownership token. Incus projects, instances, managed networks,
and custom volumes carry the matching token in `user.cloin.incus_compose.owner`.
The collection does not store rendered manifests or environment values there.
Each application must own a distinct project; the shared `default` project
cannot be an application project. Back up the inventory with the execution
host; if it is lost, restore it before pruning because live markers alone do
not reconstruct resource placement.

Before enabling pruning on an existing deployment, run the application or
catalog role once with all current definitions and
`incus_compose_enroll_existing: true` to enroll those resources, then
inspect the inventory and ownership markers. If a failed first run left an
untagged resource, inspect it and use the same enrollment flag on the retry.
Enable pruning only when the caller has confirmed it loaded the **complete
authoritative catalog**:

```yaml
- role: cloin.incus_compose.catalog
  incus_compose_catalog_directory: "{{ playbook_dir }}/../apps"
  incus_compose_prune: true
  incus_compose_catalog_complete: true
```

Pruning refuses an empty catalog, shared project ownership, conflicting
network ownership, and removal of a network still declared by a desired
application. A pending record for an absent application also blocks pruning
until the failed reconciliation is examined. Pruning verifies each live
resource's ownership marker before removal.
Removing a file preserves **all** custom volumes by default, including volumes
that previously declared `retain: false`; set `incus_compose_destroy_data: true`
to delete them during that run. The catalog result includes `retired`,
`retained_volumes`, and `retained_projects` for downstream ingress or provider
cleanup. A retained project is marked retired and is not pruned again.

To retire an enrolled application after its manifest is gone, use the `retire`
role with `incus_compose_retire_name` and the same connection and inventory
path. Set `incus_compose_destroy_data: true` only when its retained volumes
should also be deleted:

```yaml
- role: cloin.incus_compose.retire
  incus_compose_retire_name: old-app
  incus_compose_connection: "{{ incus_compose_connection }}"
```

The `incus_compose_retirement_result` fact reports the application and retained
volumes/project. Explicit `state: absent` uses the same bounded stop and remove
path. `incus_compose_stop_timeout` defaults to 30 seconds; a failed stop fails
retirement and leaves the inventory available for a retry.

See [the complete manifest reference](docs/application-schema.md).

## Cluster behavior

The collection detects whether the remote is clustered; it does not create or
join cluster members, storage pools, or OVN infrastructure. A clustered
application must declare `placement.member`, and per-service placement may
override it. Standalone remotes reject placement so a manifest cannot silently
pretend it was honored.

Managed cluster networks use Incus's two-phase creation API: the role creates a
pending definition on every online member using optional `member_config`, then
finalizes the global network. Native bridge networks can only serve instances
placed on one member; catalog validation rejects a bridge spanning members.

For custom volumes, local storage drivers (`dir`, `zfs`, `btrfs`, and `lvm`)
are targeted to the volume's explicit member or the application member. Shared
storage drivers omit the target. Explicit volume placement on shared storage is
rejected because it would be misleading.

## Extension metadata

Unknown schema fields are errors. Keys beginning with `x-` are accepted at any
strictly validated level so a consuming project can attach namespaced metadata
such as secret requirements without coupling this collection to a vault,
reverse proxy, DNS provider, or CI system. The roles ignore extension metadata.

## Development

```console
python -m pytest -q
ansible-lint
ansible-galaxy collection build --force
```

The collection is licensed under the [MIT License](LICENSE).
