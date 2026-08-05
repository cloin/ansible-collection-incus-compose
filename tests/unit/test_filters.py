from __future__ import annotations

import pathlib
import sys

import pytest
from ansible.errors import AnsibleFilterError

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "plugins" / "filter"))

from incus_compose import (  # noqa: E402
    incus_app_exposure_device,
    incus_app_network_name,
    incus_compose_catalog_order,
    incus_compose_normalize_application,
    incus_compose_route_bindings,
    incus_compose_route_count,
    incus_compose_service_order,
    incus_compose_validate_catalog,
)


def _catalog() -> list[dict]:
    return [
        {
            "schema": "cloin.incus-compose/v1",
            "name": "consumer",
            "defaults": {"network": "private"},
            "services": {
                "database": {
                    "type": "oci",
                    "image": "docker:postgres:16",
                    "networks": ["private", "ingress"],
                    "routes": {
                        "host": "database.lab.example",
                        "port": 5432,
                    },
                },
                "web": {
                    "type": "oci",
                    "image": "docker:example/web:1",
                    "depends_on": ["database"],
                    "networks": [
                        "private",
                        {"name": "ingress", "ipv4_address": "10.20.30.20"},
                    ],
                    "routes": {"host": "web.lab.example", "port": 3000},
                },
            },
            "networks": {
                "private": {"config": {"ipv4.address": "10.20.20.1/24"}},
                "ingress": {"external": True, "name": "ingress"},
            },
            "x-incus-apps": {"secrets": [{"path": "apps/consumer"}]},
        },
        {
            "schema": "cloin.incus-compose/v1",
            "name": "ingress",
            "services": {
                "proxy": {
                    "type": "container",
                    "image": "images:debian/13",
                    "networks": ["ingress"],
                }
            },
            "networks": {
                "ingress": {
                    "name": "ingress",
                    "config": {"ipv4.address": "10.20.30.1/24"},
                }
            },
        },
    ]


def test_normalizes_strict_public_schema_and_accepts_extensions():
    definition = _catalog()[0]
    normalized = incus_compose_normalize_application(definition)

    assert normalized["application"]["name"] == "consumer"
    assert normalized["services"]["web"]["kind"] == "application-container"
    assert normalized["services"]["web"]["ingress"][0]["network"] == "ingress"


def test_rejects_unknown_core_and_nested_keys():
    definition = _catalog()[0]
    definition["servcies"] = {}
    with pytest.raises(AnsibleFilterError, match="unsupported keys: servcies"):
        incus_compose_validate_catalog([definition])

    definition = _catalog()[0]
    definition["services"]["web"]["routes"]["prot"] = 3000
    with pytest.raises(AnsibleFilterError, match="unsupported keys: prot"):
        incus_compose_validate_catalog([definition])


def test_catalog_orders_shared_network_owner_first():
    ordered = incus_compose_catalog_order(_catalog())
    assert [item["name"] for item in ordered] == ["ingress", "consumer"]


def test_service_order_is_stable_and_rejects_cycles():
    services = _catalog()[0]["services"]
    assert incus_compose_service_order(services) == ["database", "web"]
    services["database"]["depends_on"] = ["web"]
    with pytest.raises(AnsibleFilterError, match="dependency cycle"):
        incus_compose_service_order(services)


def test_network_names_are_namespaced_unless_external():
    assert incus_app_network_name("gitea", "private", {}) == "gitea-private"
    assert incus_app_network_name("longapplication", "private", {}) == "longappl-61c6cc"
    assert incus_app_network_name("app", "proxy", {"external": True}) == "proxy"


def test_any_service_can_declare_a_route():
    assert incus_compose_validate_catalog(_catalog()) is True
    assert incus_compose_route_count(_catalog()) == 2


def test_rejects_duplicate_routes_addresses_and_ports():
    catalog = _catalog()
    catalog[0]["services"]["database"]["routes"]["host"] = "web.lab.example"
    with pytest.raises(AnsibleFilterError, match="HTTP ingress host"):
        incus_compose_validate_catalog(catalog)

    catalog = _catalog()
    catalog[0]["services"]["database"]["networks"][1] = {
        "name": "ingress",
        "ipv4_address": "10.20.30.20",
    }
    with pytest.raises(AnsibleFilterError, match="explicit address"):
        incus_compose_validate_catalog(catalog)

    catalog = _catalog()
    catalog[0]["services"]["database"]["ports"] = {
        "name": "one",
        "listen_port": 2222,
        "target_port": 22,
    }
    catalog[0]["services"]["web"]["ports"] = {
        "name": "two",
        "listen_address": "192.0.2.1",
        "listen_port": 2222,
        "target_port": 22,
    }
    with pytest.raises(AnsibleFilterError, match="collides"):
        incus_compose_validate_catalog(catalog)


def test_bridge_network_cannot_span_cluster_members():
    catalog = _catalog()
    catalog[1]["placement"] = {"member": "node1"}
    catalog[0]["placement"] = {"member": "node2"}
    with pytest.raises(AnsibleFilterError, match="bridge network.*spans"):
        incus_compose_validate_catalog(catalog)


def test_explicit_volume_placement_remains_explicit():
    definition = _catalog()[0]
    definition["placement"] = {"member": "node1"}
    definition["volumes"] = {
        "implicit": {"pool": "local"},
        "explicit": {"pool": "local", "placement": {"member": "node2"}},
    }
    volumes = incus_compose_normalize_application(definition)["volumes"]
    assert "target" not in volumes["implicit"]
    assert volumes["explicit"]["target"] == "node2"


def test_port_becomes_incus_proxy_device():
    assert incus_app_exposure_device(
        {"name": "dns", "protocol": "udp", "listen_port": 53, "target_port": 5353}
    ) == {
        "name": "dns",
        "type": "proxy",
        "listen": "udp:0.0.0.0:53",
        "connect": "udp:127.0.0.1:5353",
    }


def test_routes_resolve_from_live_incus_state():
    instances = [
        {
            "project": "consumer",
            "name": name,
            "status": "Running",
            "expanded_devices": {
                "eth0": {"type": "nic", "network": "consumer-private"},
                "eth1": {"type": "nic", "network": "ingress"},
            },
            "state": {
                "network": {
                    "eth1": {
                        "addresses": [
                            {
                                "family": "inet",
                                "scope": "global",
                                "address": address,
                            }
                        ]
                    }
                }
            },
        }
        for name, address in (("database", "10.20.30.21"), ("web", "10.20.30.20"))
    ]
    bindings = incus_compose_route_bindings(_catalog(), instances)
    assert [item["host"] for item in bindings] == [
        "database.lab.example",
        "web.lab.example",
    ]
    assert bindings[1]["address"] == "10.20.30.20"
