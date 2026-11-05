# Application schema

Every file is one `cloin.incus-compose/v1` document. Core fields are strict;
misspellings fail before Incus is changed. Any strict mapping may also contain
consumer-owned `x-*` metadata.

```yaml
---
schema: cloin.incus-compose/v1
name: linkwarden
description: Self-hosted bookmark manager
state: present

# Required only when the target Incus server is clustered.
placement:
  member: node1

project:
  # Defaults to the application name.
  name: linkwarden
  config:
    features.networks: false

defaults:
  storage_pool: default
  network: private
  profiles: []
  config:
    limits.memory: 2GiB

services:
  postgres:
    type: oci
    image: docker:postgres:16
    environment:
      POSTGRES_PASSWORD: "{{ linkwarden_database_password }}"
    networks: [private]
    volumes:
      postgres: /var/lib/postgresql/data

  web:
    type: oci
    image: ghcr:ghcr.io/linkwarden/linkwarden:v2.16.0
    root:
      pool: default
      size: 8GiB
    networks:
      - private
      - name: ingress
        # Optional. Omit this to use Incus DHCP/IPAM.
        ipv4_address: 10.210.250.20
    depends_on: [postgres]
    routes:
      - host: linkwarden.lab.example
        port: 3000
        scheme: http
        network: ingress
    ports:
      - name: metrics
        protocol: tcp
        listen_address: 192.0.2.10
        listen_port: 9101
        target_port: 9100

networks:
  private:
    type: bridge
    config:
      ipv4.address: 10.210.2.1/24
      ipv4.nat: true
      ipv6.address: none
    member_config:
      node1: {}
      node2: {}

  ingress:
    external: true
    name: ingress

volumes:
  postgres:
    pool: default
    size: 20GiB
    retain: true
    placement:
      member: node1
    config:
      snapshots.schedule: "@daily"

x-example:
  owner: lab
```

## Services

`type` is `oci`, `container`, or `vm`. `image` and `source` use Incus remote
notation. `placement.member` overrides application placement. `root` accepts
`pool` and `size`. Native `profiles`, `config`, and `devices` remain available.

`networks` contains logical names or mappings with `name`, `ipv4_address`, and
`ipv6_address`. `volumes` maps a logical application volume to an absolute
guest path or `{path, readonly}`. `depends_on` is a list or mapping and is
topologically sorted. Unknown dependencies and cycles fail validation.

`healthcheck` supports `command` or `http`, expected `status`, `interval`, and
`retries`. System containers can request `network_manager:
systemd-networkd`. Update behavior is controlled with `update.strategy` and an
optional command.

## Routes and ports

`routes` describes HTTP routing without selecting a reverse proxy. Each entry
requires a unique FQDN `host` and upstream `port`; `scheme` defaults to `http`
and `network` defaults to `ingress`. Any service may declare a route when that
is intentional. The catalog resolves each route to the instance's current live
IPv4 address on that network.

`ports` describes explicit Incus proxy devices for TCP or UDP. Each entry has a
device `name`, `listen_port`, and `target_port`; protocol and addresses are
optional. Catalog validation rejects overlapping listeners, including wildcard
address collisions. Stable explicit ports are intentional—random host ports
would make firewall and client configuration nondeterministic.

## Networks and IPAM

Managed networks are application-namespaced unless `name` is set. An external
network must already exist or be owned by another catalog application. The
catalog automatically reconciles a network owner before its consumers.

Omit NIC address fields in the normal case. A managed Incus bridge provides
DHCP and IPAM, and route resolution reads the live allocation. A declared
address becomes an Incus NIC reservation. Validation rejects duplicate
addresses on the same physical network and addresses outside a managed subnet.

## Destruction

`state: absent` removes services and managed networks. Custom volumes default
to `retain: true`; they and the project remain until
`incus_compose_destroy_data: true` is deliberately supplied. External networks
are never removed by their consumers.

Services are stopped gracefully in reverse dependency order before deletion.
`incus_compose_stop_timeout` bounds the stop attempt. Automatic catalog pruning
has a separate data policy: it preserves every custom volume by default,
regardless of the manifest's `retain` field. See the README for enrollment,
prune guards, targeted retirement, and result fields.
