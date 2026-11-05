"""Filters used by the cloin.incus_compose collection."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean

VALID_KINDS = {
    "application-container",
    "system-container",
    "virtual-machine",
}

VALID_INGRESS_SCHEMES = {"http", "https"}
VALID_EXPOSURE_PROTOCOLS = {"tcp", "udp"}
SCHEMA_ID = "cloin.incus-compose/v1"
SERVICE_TYPE_TO_KIND = {
    "oci": "application-container",
    "container": "system-container",
    "vm": "virtual-machine",
}
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def incus_app_is_ipv4_address(value) -> bool:
    """Return whether a value is one unambiguous IPv4 address."""
    try:
        return ipaddress.ip_address(str(value)).version == 4
    except ValueError:
        return False


def incus_app_is_private_ipv4_prefix(value) -> bool:
    """Validate the two-octet RFC1918 prefix used by bundled applications."""
    parts = str(value).split(".")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        return False
    if any(part != str(int(part)) or not 0 <= int(part) <= 255 for part in parts):
        return False
    address = ipaddress.ip_address(f"{value}.0.0")
    return any(address in network for network in RFC1918_NETWORKS)


def _application_context(definition: Mapping) -> tuple[Mapping, Mapping, Mapping]:
    if not isinstance(definition, Mapping):
        raise AnsibleFilterError("each application definition must be a mapping")

    application = definition.get("application")
    if not isinstance(application, Mapping) or not application.get("name"):
        raise AnsibleFilterError("application.name is required")

    services = definition.get("services", {})
    networks = definition.get("networks", {})
    if not isinstance(services, Mapping):
        raise AnsibleFilterError(
            f"application '{application['name']}'.services must be a mapping"
        )
    if not isinstance(networks, Mapping):
        raise AnsibleFilterError(
            f"application '{application['name']}'.networks must be a mapping"
        )
    for name, network in networks.items():
        if not isinstance(network, Mapping):
            raise AnsibleFilterError(
                f"application '{application['name']}' network '{name}' must be a mapping"
            )
    return application, services, networks


def incus_app_network_attachment(value) -> dict:
    """Normalize a service network entry while retaining legacy strings."""
    if isinstance(value, str):
        if not value:
            raise AnsibleFilterError("service network names cannot be empty")
        return {"name": value}
    if not isinstance(value, Mapping) or not value.get("name"):
        raise AnsibleFilterError(
            "each service network must be a name or a mapping with a name"
        )

    result = dict(value)
    for family in ("ipv4", "ipv6"):
        key = f"{family}_address"
        if key not in result:
            continue
        try:
            address = ipaddress.ip_address(str(result[key]))
        except ValueError as exc:
            raise AnsibleFilterError(
                f"service network {key} '{result[key]}' is invalid"
            ) from exc
        expected_version = 4 if family == "ipv4" else 6
        if address.version != expected_version:
            raise AnsibleFilterError(
                f"service network {key} must be an IPv{expected_version} address"
            )
        result[key] = str(address)
    return result


def _service_networks(service: Mapping, defaults: Mapping) -> list[dict]:
    raw_networks = service.get("networks")
    if raw_networks is None:
        raw_networks = [defaults["network"]] if defaults.get("network") else []
    if not isinstance(raw_networks, list):
        raise AnsibleFilterError("service.networks must be a list")

    attachments = [incus_app_network_attachment(item) for item in raw_networks]
    names = [item["name"] for item in attachments]
    if len(names) != len(set(names)):
        raise AnsibleFilterError("a service cannot attach the same network more than once")
    return attachments


def _mapping_list(value, label: str) -> list[dict]:
    if value is None:
        return []
    values = [value] if isinstance(value, Mapping) else value
    if not isinstance(values, list) or any(
        not isinstance(item, Mapping) for item in values
    ):
        raise AnsibleFilterError(f"{label} must be a mapping or list of mappings")
    return [dict(item) for item in values]


def _port(value, label: str) -> int:
    if isinstance(value, bool):
        raise AnsibleFilterError(f"{label} must be an integer between 1 and 65535")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AnsibleFilterError(
            f"{label} must be an integer between 1 and 65535"
        ) from exc
    if result < 1 or result > 65535:
        raise AnsibleFilterError(f"{label} must be an integer between 1 and 65535")
    return result


def _ingress_entries(service: Mapping, label: str) -> list[dict]:
    entries = _mapping_list(service.get("ingress"), f"{label}.ingress")
    for entry in entries:
        host = str(entry.get("host", "")).lower()
        if not re.fullmatch(
            r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
            host,
        ):
            raise AnsibleFilterError(f"{label}.ingress host '{host}' is not a valid FQDN")
        entry["host"] = host
        entry["port"] = _port(entry.get("port"), f"{label}.ingress.port")
        scheme = str(entry.get("scheme", "http")).lower()
        if scheme not in VALID_INGRESS_SCHEMES:
            raise AnsibleFilterError(
                f"{label}.ingress.scheme must be one of {sorted(VALID_INGRESS_SCHEMES)}"
            )
        entry["scheme"] = scheme
        entry["network"] = str(entry.get("network", "ingress"))
    return entries


def _exposure_entries(service: Mapping, label: str) -> list[dict]:
    entries = _mapping_list(service.get("expose"), f"{label}.expose")
    names: set[str] = set()
    for entry in entries:
        name = str(entry.get("name", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise AnsibleFilterError(
                f"{label}.expose.name must contain lowercase letters, digits, or hyphens"
            )
        if name in names:
            raise AnsibleFilterError(f"{label}.expose name '{name}' is duplicated")
        names.add(name)

        protocol = str(entry.get("protocol", "tcp")).lower()
        if protocol not in VALID_EXPOSURE_PROTOCOLS:
            raise AnsibleFilterError(
                f"{label}.expose.protocol must be one of "
                f"{sorted(VALID_EXPOSURE_PROTOCOLS)}"
            )
        entry["protocol"] = protocol
        entry["listen_port"] = _port(
            entry.get("listen_port"), f"{label}.expose.listen_port"
        )
        entry["target_port"] = _port(
            entry.get("target_port"), f"{label}.expose.target_port"
        )
        for key, default in (
            ("listen_address", "0.0.0.0"),
            ("target_address", "127.0.0.1"),
        ):
            raw_address = str(entry.get(key, default))
            try:
                entry[key] = str(ipaddress.ip_address(raw_address))
            except ValueError as exc:
                raise AnsibleFilterError(
                    f"{label}.expose.{key} '{raw_address}' is invalid"
                ) from exc
    return entries


def _physical_network_name(
    application_name: str, logical_name: str, networks: Mapping
) -> str:
    if logical_name not in networks:
        raise AnsibleFilterError(
            f"application '{application_name}' references undefined network "
            f"'{logical_name}'"
        )
    return incus_app_network_name(
        application_name, logical_name, networks[logical_name]
    )


def incus_app_validate_catalog(definitions: list) -> bool:
    """Validate cross-application network and ingress invariants."""
    if not isinstance(definitions, list):
        raise AnsibleFilterError("the application catalog must be a list")

    explicit_addresses: dict[tuple[str, str], str] = {}
    ingress_hosts: dict[str, str] = {}
    exposures: list[tuple[str, int, str, str]] = []
    managed_networks: dict[str, Mapping] = {}

    contexts = []
    for definition in definitions:
        application, services, networks = _application_context(definition)
        app_name = str(application["name"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", app_name):
            raise AnsibleFilterError(f"application name '{app_name}' is invalid")
        incus_app_service_order(services)
        contexts.append((definition, application, services, networks))

        for logical_name, network in networks.items():
            if not network.get("external", False):
                physical_name = incus_app_network_name(app_name, logical_name, network)
                previous = managed_networks.get(physical_name)
                if previous is not None and previous != network:
                    raise AnsibleFilterError(
                        f"managed network '{physical_name}' has conflicting definitions"
                    )
                managed_networks[physical_name] = network

    for _definition, application, services, networks in contexts:
        app_name = str(application["name"])
        defaults = application.get("defaults", {})
        if not isinstance(defaults, Mapping):
            raise AnsibleFilterError(f"application '{app_name}'.defaults must be a mapping")

        for service_name, service in services.items():
            label = f"application '{app_name}' service '{service_name}'"
            if not isinstance(service, Mapping):
                raise AnsibleFilterError(f"{label} must be a mapping")
            if service.get("network_manager") not in (None, "systemd-networkd"):
                raise AnsibleFilterError(
                    f"{label}.network_manager must be 'systemd-networkd' when set"
                )
            attachments = _service_networks(service, defaults)
            attachment_names = {item["name"] for item in attachments}
            for attachment in attachments:
                logical_name = attachment["name"]
                physical_name = _physical_network_name(app_name, logical_name, networks)
                for key in ("ipv4_address", "ipv6_address"):
                    if key not in attachment:
                        continue
                    address = attachment[key]
                    address_key = (physical_name, address)
                    if address_key in explicit_addresses:
                        raise AnsibleFilterError(
                            f"explicit address {address} on network '{physical_name}' "
                            f"is used by both {explicit_addresses[address_key]} and {label}"
                        )
                    explicit_addresses[address_key] = label

                    network = managed_networks.get(physical_name)
                    family = "ipv4" if key.startswith("ipv4") else "ipv6"
                    subnet_value = (network or {}).get("config", {}).get(
                        f"{family}.address"
                    )
                    if subnet_value and subnet_value not in ("auto", "none"):
                        subnet = ipaddress.ip_interface(str(subnet_value)).network
                        if ipaddress.ip_address(address) not in subnet:
                            raise AnsibleFilterError(
                                f"explicit address {address} is outside network "
                                f"'{physical_name}' subnet {subnet}"
                            )

            native_names = {
                str(device.get("name"))
                for device in service.get("devices", [])
                if isinstance(device, Mapping) and device.get("name")
            }
            for entry in _exposure_entries(service, label):
                if entry["name"] in native_names:
                    raise AnsibleFilterError(
                        f"{label} uses device name '{entry['name']}' in both expose and devices"
                    )
                current = (
                    entry["protocol"],
                    entry["listen_port"],
                    entry["listen_address"],
                    label,
                )
                for protocol, port, address, owner in exposures:
                    wildcard = {"0.0.0.0", "::"}
                    if (
                        protocol == current[0]
                        and port == current[1]
                        and (
                            address == current[2]
                            or address in wildcard
                            or current[2] in wildcard
                        )
                    ):
                        raise AnsibleFilterError(
                            f"raw exposure {protocol}:{address}:{port} for {owner} "
                            f"collides with {label}"
                        )
                exposures.append(current)

            for entry in _ingress_entries(service, label):
                if entry["network"] not in attachment_names:
                    raise AnsibleFilterError(
                        f"{label} declares ingress on network '{entry['network']}' "
                        "but is not attached to it"
                    )
                host = entry["host"]
                if host in ingress_hosts:
                    raise AnsibleFilterError(
                        f"HTTP ingress host '{host}' is declared by both "
                        f"{ingress_hosts[host]} and {label}"
                    )
                ingress_hosts[host] = label
    return True


def _format_proxy_address(address: str, port: int) -> str:
    parsed = ipaddress.ip_address(address)
    return f"[{parsed}]:{port}" if parsed.version == 6 else f"{parsed}:{port}"


def incus_app_exposure_device(exposure: Mapping) -> dict:
    """Convert a structured TCP/UDP exposure into an Incus proxy device."""
    entries = _exposure_entries({"expose": exposure}, "service")
    if len(entries) != 1:
        raise AnsibleFilterError("one exposure mapping is required")
    entry = entries[0]
    device = {
        "name": entry["name"],
        "type": "proxy",
        "listen": (
            f"{entry['protocol']}:"
            f"{_format_proxy_address(entry['listen_address'], entry['listen_port'])}"
        ),
        "connect": (
            f"{entry['protocol']}:"
            f"{_format_proxy_address(entry['target_address'], entry['target_port'])}"
        ),
    }
    for key in ("bind", "gid", "mode", "nat", "proxy_protocol", "security.gid"):
        if key in exposure:
            device[key] = exposure[key]
    return device


def incus_app_ingress_count(definitions: list) -> int:
    """Return the number of desired running HTTP ingress routes."""
    incus_app_validate_catalog(definitions)
    count = 0
    for definition in definitions:
        application, services, _networks = _application_context(definition)
        if application.get("state", "present") != "present":
            continue
        for service_name, service in services.items():
            if service.get("state", "started") != "started":
                continue
            count += len(
                _ingress_entries(
                    service,
                    f"application '{application['name']}' service '{service_name}'",
                )
            )
    return count


def incus_app_ingress_bindings(
    definitions: list, instances: list, strict: bool = True
) -> list[dict]:
    """Resolve desired HTTP routes to live addresses reported by Incus."""
    incus_app_validate_catalog(definitions)
    if not isinstance(instances, list):
        raise AnsibleFilterError("live Incus instances must be a list")
    instance_index = {
        (str(instance.get("project", "default")), str(instance.get("name", ""))): instance
        for instance in instances
        if isinstance(instance, Mapping)
    }
    bindings = []

    for definition in definitions:
        application, services, networks = _application_context(definition)
        if application.get("state", "present") != "present":
            continue
        app_name = str(application["name"])
        project = str(application.get("project") or app_name)
        defaults = application.get("defaults", {})
        for service_name, service in services.items():
            if service.get("state", "started") != "started":
                continue
            label = f"application '{app_name}' service '{service_name}'"
            entries = _ingress_entries(service, label)
            if not entries:
                continue
            instance_name = str(service.get("name") or service_name)
            instance = instance_index.get((project, instance_name))
            if not instance or instance.get("status") != "Running":
                if strict:
                    raise AnsibleFilterError(f"{label} is not running in Incus")
                continue

            attachments = {
                item["name"]: item for item in _service_networks(service, defaults)
            }
            expanded_devices = instance.get("expanded_devices", {})
            state_networks = (instance.get("state") or {}).get("network", {})
            for entry in entries:
                logical_network = entry["network"]
                if logical_network not in attachments:
                    if strict:
                        raise AnsibleFilterError(
                            f"{label} is not attached to ingress network "
                            f"'{logical_network}'"
                        )
                    continue
                physical_network = _physical_network_name(
                    app_name, logical_network, networks
                )
                matching_devices = [
                    name
                    for name, device in expanded_devices.items()
                    if device.get("type") == "nic"
                    and device.get("network") == physical_network
                ]
                addresses = []
                if len(matching_devices) == 1:
                    addresses = [
                        address.get("address")
                        for address in state_networks.get(matching_devices[0], {}).get(
                            "addresses", []
                        )
                        if address.get("family") == "inet"
                        and address.get("scope") == "global"
                    ]
                if len(addresses) != 1:
                    if strict:
                        raise AnsibleFilterError(
                            f"{label} does not have exactly one live IPv4 address on "
                            f"network '{physical_network}'"
                        )
                    continue
                bindings.append(
                    {
                        "application": app_name,
                        "project": project,
                        "service": str(service_name),
                        "instance": instance_name,
                        "host": entry["host"],
                        "scheme": entry["scheme"],
                        "port": entry["port"],
                        "network": physical_network,
                        "address": addresses[0],
                    }
                )
    return sorted(bindings, key=lambda item: item["host"])


def incus_app_service_order(services: Mapping) -> list[str]:
    """Return a stable topological ordering of an application's services."""
    if not isinstance(services, Mapping):
        raise AnsibleFilterError("application.services must be a mapping")

    service_names = list(services)
    dependencies: dict[str, list[str]] = {}

    for name, definition in services.items():
        if not isinstance(definition, Mapping):
            raise AnsibleFilterError(f"service '{name}' must be a mapping")

        kind = definition.get("kind", "application-container")
        if kind not in VALID_KINDS:
            raise AnsibleFilterError(
                f"service '{name}' has unsupported kind '{kind}'; "
                f"expected one of {sorted(VALID_KINDS)}"
            )

        raw_dependencies = definition.get("depends_on", {})
        if isinstance(raw_dependencies, list):
            service_dependencies = raw_dependencies
        elif isinstance(raw_dependencies, Mapping):
            service_dependencies = list(raw_dependencies)
        else:
            raise AnsibleFilterError(
                f"service '{name}'.depends_on must be a list or mapping"
            )

        unknown = [item for item in service_dependencies if item not in services]
        if unknown:
            raise AnsibleFilterError(
                f"service '{name}' depends on unknown services: {', '.join(unknown)}"
            )
        dependencies[name] = service_dependencies

    ordered: list[str] = []
    remaining = service_names.copy()
    while remaining:
        ready = [
            name
            for name in remaining
            if all(dependency in ordered for dependency in dependencies[name])
        ]
        if not ready:
            raise AnsibleFilterError(
                "application.services contains a dependency cycle involving: "
                + ", ".join(remaining)
            )
        for name in ready:
            ordered.append(name)
            remaining.remove(name)

    return ordered


def incus_app_network_name(application_name: str, logical_name: str, definition: Mapping) -> str:
    """Resolve a logical network to its physical Incus network name."""
    if definition.get("external", False):
        return definition.get("name", logical_name)
    configured_name = definition.get("name")
    if configured_name:
        return configured_name

    candidate = f"{application_name}-{logical_name}"
    if len(candidate) <= 15:
        return candidate

    suffix = hashlib.sha256(candidate.encode()).hexdigest()[:6]
    return f"{candidate[:8].rstrip('-')}-{suffix}"


def incus_app_oci_defaults(
    current_config: Mapping, desired_environment: Mapping
) -> dict:
    """Preserve OCI image defaults and overlay application environment values.

    Incus materializes an OCI image's entrypoint, uid/gid, working directory,
    and environment in instance configuration.  The damex.incus instance
    module manages configuration strictly, so callers must include those
    image-derived values on every subsequent update.
    """
    if not isinstance(current_config, Mapping):
        raise AnsibleFilterError("current OCI instance config must be a mapping")
    if not isinstance(desired_environment, Mapping):
        raise AnsibleFilterError("service.environment must be a mapping")

    result = {
        key: value
        for key, value in current_config.items()
        if isinstance(key, str) and key.startswith("oci.")
    }
    environment = {
        key.removeprefix("environment."): value
        for key, value in current_config.items()
        if isinstance(key, str) and key.startswith("environment.")
    }
    environment.update(
        {str(key): str(value) for key, value in desired_environment.items()}
    )
    if environment:
        result["environment_variables"] = [
            {"name": name, "value": str(environment[name])}
            for name in sorted(environment)
        ]
    return result


def _incus_string(value) -> str:
    """Render an Incus config/device scalar the same way as its API."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def incus_app_oci_raw_config(
    current_config: Mapping,
    desired_environment: Mapping,
    native_config: Mapping,
) -> dict[str, str]:
    """Build a complete raw Incus config for an OCI instance API update."""
    if not isinstance(current_config, Mapping):
        raise AnsibleFilterError("current OCI instance config must be a mapping")
    if not isinstance(desired_environment, Mapping):
        raise AnsibleFilterError("service.environment must be a mapping")
    if not isinstance(native_config, Mapping):
        raise AnsibleFilterError("native instance config must be a mapping")

    preserved_prefixes = ("image.", "volatile.", "oci.", "environment.")
    result = {
        str(key): _incus_string(value)
        for key, value in current_config.items()
        if isinstance(key, str) and key.startswith(preserved_prefixes)
    }
    for name, value in desired_environment.items():
        result[f"environment.{name}"] = _incus_string(value)
    for key, value in native_config.items():
        if key == "environment_variables":
            continue
        if value is not None:
            result[str(key)] = _incus_string(value)
    return result


def incus_app_devices_config(devices: list) -> dict[str, dict[str, str]]:
    """Convert the role's named device list to the Incus API device mapping."""
    if not isinstance(devices, list):
        raise AnsibleFilterError("instance devices must be a list")

    result = {}
    for device in devices:
        if not isinstance(device, Mapping) or not device.get("name"):
            raise AnsibleFilterError("every instance device must be a mapping with a name")
        result[str(device["name"])] = {
            str(key): _incus_string(value)
            for key, value in device.items()
            if key != "name" and value is not None
        }
    return result


def _strict_keys(value: Mapping, allowed: set[str], label: str) -> None:
    """Reject misspelled core keys while allowing namespaced extensions."""

    unknown = sorted(
        str(key)
        for key in value
        if key not in allowed and not str(key).startswith("x-")
    )
    if unknown:
        raise AnsibleFilterError(
            f"{label} contains unsupported keys: {', '.join(unknown)}"
        )


def _mapping(value, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AnsibleFilterError(f"{label} must be a mapping")
    return dict(value)


def _placement(value, label: str) -> dict:
    result = _mapping(value, label)
    _strict_keys(result, {"member"}, label)
    if "member" in result:
        member = str(result["member"])
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", member):
            raise AnsibleFilterError(f"{label}.member '{member}' is invalid")
        result["member"] = member
    return result


def _normalize_network_attachment_v1(value, label: str) -> dict | str:
    if isinstance(value, str):
        return value
    attachment = _mapping(value, label)
    _strict_keys(
        attachment,
        {"name", "ipv4_address", "ipv6_address"},
        label,
    )
    incus_app_network_attachment(attachment)
    return attachment


def _normalize_ports_v1(value, label: str) -> list[dict]:
    entries = _mapping_list(value, label)
    allowed = {
        "name",
        "protocol",
        "listen_address",
        "listen_port",
        "target_address",
        "target_port",
        "bind",
        "gid",
        "mode",
        "nat",
        "proxy_protocol",
        "security.gid",
    }
    for index, entry in enumerate(entries):
        _strict_keys(entry, allowed, f"{label}[{index}]")
    return _exposure_entries({"expose": entries}, label.removesuffix(".ports"))


def _normalize_routes_v1(value, label: str) -> list[dict]:
    entries = _mapping_list(value, label)
    for index, entry in enumerate(entries):
        _strict_keys(
            entry,
            {"host", "port", "scheme", "network"},
            f"{label}[{index}]",
        )
    return _ingress_entries({"ingress": entries}, label.removesuffix(".routes"))


def _normalize_service_v1(
    service_name: str,
    value: Mapping,
    application_member: str | None,
) -> dict:
    label = f"service '{service_name}'"
    service = _mapping(value, label)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", service_name):
        raise AnsibleFilterError(f"service name '{service_name}' is invalid")
    _strict_keys(
        service,
        {
            "name",
            "type",
            "image",
            "source",
            "source_server",
            "source_protocol",
            "placement",
            "state",
            "root",
            "profiles",
            "ephemeral",
            "config",
            "environment",
            "networks",
            "volumes",
            "devices",
            "ports",
            "routes",
            "depends_on",
            "healthcheck",
            "update",
            "recreate_on_source_change",
            "network_manager",
        },
        label,
    )

    service_type = str(service.get("type", "oci"))
    if service_type not in SERVICE_TYPE_TO_KIND:
        raise AnsibleFilterError(
            f"{label}.type must be one of {sorted(SERVICE_TYPE_TO_KIND)}"
        )
    root = _mapping(service.get("root"), f"{label}.root")
    _strict_keys(root, {"pool", "size"}, f"{label}.root")
    placement = _placement(service.get("placement"), f"{label}.placement")
    target = placement.get("member", application_member)

    for key in ("config", "environment"):
        _mapping(service.get(key), f"{label}.{key}")
    profiles = service.get("profiles", [])
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        raise AnsibleFilterError(f"{label}.profiles must be a list of names")
    volumes = _mapping(service.get("volumes"), f"{label}.volumes")
    for volume_name, attachment in volumes.items():
        attachment_label = f"{label}.volumes.{volume_name}"
        if isinstance(attachment, str):
            if not attachment.startswith("/"):
                raise AnsibleFilterError(f"{attachment_label} must be an absolute path")
            continue
        attachment = _mapping(attachment, attachment_label)
        _strict_keys(attachment, {"path", "readonly"}, attachment_label)
        if not str(attachment.get("path", "")).startswith("/"):
            raise AnsibleFilterError(f"{attachment_label}.path must be an absolute path")
    devices = service.get("devices", [])
    if not isinstance(devices, list):
        raise AnsibleFilterError(f"{label}.devices must be a list")
    incus_app_devices_config(devices)

    networks = service.get("networks")
    if networks is not None:
        if not isinstance(networks, list):
            raise AnsibleFilterError(f"{label}.networks must be a list")
        networks = [
            _normalize_network_attachment_v1(item, f"{label}.networks")
            for item in networks
        ]

    healthcheck = _mapping(service.get("healthcheck"), f"{label}.healthcheck")
    _strict_keys(
        healthcheck,
        {"command", "http", "status", "interval", "retries", "validate_certs"},
        f"{label}.healthcheck",
    )
    update = _mapping(service.get("update"), f"{label}.update")
    _strict_keys(update, {"strategy", "command"}, f"{label}.update")
    if update.get("strategy") not in (None, "packages", "recreate"):
        raise AnsibleFilterError(
            f"{label}.update.strategy must be packages or recreate"
        )
    if service.get("state", "started") not in {"started", "stopped"}:
        raise AnsibleFilterError(f"{label}.state must be started or stopped")
    ports = _normalize_ports_v1(service.get("ports"), f"{label}.ports")
    routes = _normalize_routes_v1(service.get("routes"), f"{label}.routes")

    result = {
        key: item
        for key, item in service.items()
        if key
        not in {
            "type",
            "placement",
            "root",
            "ports",
            "routes",
        }
        and not str(key).startswith("x-")
    }
    result["kind"] = SERVICE_TYPE_TO_KIND[service_type]
    if target:
        result["target"] = target
    if root.get("pool") is not None:
        result["root_pool"] = root["pool"]
    if root.get("size") is not None:
        result["root_size"] = root["size"]
    if networks is not None:
        result["networks"] = networks
    if healthcheck:
        result["healthcheck"] = healthcheck
    if update:
        result["update"] = update
    if ports:
        result["expose"] = ports
    if routes:
        result["ingress"] = routes
    return result


def incus_compose_normalize_application(definition: Mapping) -> dict:
    """Validate and translate one public v1 manifest for the role internals."""

    manifest = _mapping(definition, "application definition")
    _strict_keys(
        manifest,
        {
            "schema",
            "name",
            "description",
            "state",
            "project",
            "placement",
            "defaults",
            "services",
            "networks",
            "volumes",
        },
        "application definition",
    )
    if manifest.get("schema") != SCHEMA_ID:
        raise AnsibleFilterError(f"application schema must be '{SCHEMA_ID}'")
    name = str(manifest.get("name", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise AnsibleFilterError(f"application name '{name}' is invalid")
    state = str(manifest.get("state", "present"))
    if state not in {"present", "absent"}:
        raise AnsibleFilterError("application.state must be present or absent")

    project = _mapping(manifest.get("project"), f"application '{name}'.project")
    _strict_keys(project, {"name", "config"}, f"application '{name}'.project")
    project_config = _mapping(
        project.get("config"), f"application '{name}'.project.config"
    )
    project_name = str(project.get("name") or name)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", project_name):
        raise AnsibleFilterError(
            f"application '{name}'.project.name '{project_name}' is invalid"
        )
    placement = _placement(
        manifest.get("placement"), f"application '{name}'.placement"
    )
    application_member = placement.get("member")
    defaults = _mapping(
        manifest.get("defaults"), f"application '{name}'.defaults"
    )
    _strict_keys(
        defaults,
        {"storage_pool", "network", "profiles", "config"},
        f"application '{name}'.defaults",
    )

    services = _mapping(
        manifest.get("services"), f"application '{name}'.services"
    )
    normalized_services = {
        str(service_name): _normalize_service_v1(
            str(service_name), service, application_member
        )
        for service_name, service in services.items()
    }

    networks = _mapping(
        manifest.get("networks"), f"application '{name}'.networks"
    )
    normalized_networks = {}
    for logical_name, raw_network in networks.items():
        label = f"application '{name}' network '{logical_name}'"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(logical_name)):
            raise AnsibleFilterError(f"network name '{logical_name}' is invalid")
        network = _mapping(raw_network, label)
        _strict_keys(
            network,
            {
                "name",
                "type",
                "external",
                "project",
                "description",
                "config",
                "member_config",
            },
            label,
        )
        member_config = _mapping(network.get("member_config"), f"{label}.member_config")
        for member_name, member_value in member_config.items():
            member_config[member_name] = _mapping(
                member_value, f"{label}.member_config.{member_name}"
            )
        _mapping(network.get("config"), f"{label}.config")
        if network.get("type", "bridge") not in {
            "bridge",
            "macvlan",
            "ovn",
            "physical",
            "sriov",
        }:
            raise AnsibleFilterError(f"{label}.type is unsupported")
        if boolean(network.get("external", False), strict=False) and any(
            key in network
            for key in ("type", "project", "description", "config", "member_config")
        ):
            raise AnsibleFilterError(
                f"{label} is external and may only set name and extension metadata"
            )
        normalized_networks[str(logical_name)] = {
            key: item
            for key, item in network.items()
            if not str(key).startswith("x-")
        }
        if "external" in network:
            normalized_networks[str(logical_name)]["external"] = boolean(
                network["external"], strict=False
            )

    volumes = _mapping(
        manifest.get("volumes"), f"application '{name}'.volumes"
    )
    normalized_volumes = {}
    for logical_name, raw_volume in volumes.items():
        label = f"application '{name}' volume '{logical_name}'"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(logical_name)):
            raise AnsibleFilterError(f"volume name '{logical_name}' is invalid")
        volume = _mapping(raw_volume, label)
        _strict_keys(
            volume,
            {
                "name",
                "pool",
                "size",
                "retain",
                "content_type",
                "config",
                "placement",
            },
            label,
        )
        volume_placement = _placement(volume.get("placement"), f"{label}.placement")
        target = volume_placement.get("member")
        _mapping(volume.get("config"), f"{label}.config")
        if volume.get("content_type", "filesystem") not in {"filesystem", "block"}:
            raise AnsibleFilterError(
                f"{label}.content_type must be filesystem or block"
            )
        normalized_volume = {
            key: item
            for key, item in volume.items()
            if key != "placement" and not str(key).startswith("x-")
        }
        if target:
            normalized_volume["target"] = target
        if "retain" in volume:
            normalized_volume["retain"] = boolean(volume["retain"], strict=False)
        normalized_volumes[str(logical_name)] = normalized_volume

    application = {
        "name": name,
        "description": manifest.get("description", ""),
        "state": state,
        "project": project_name,
        "project_config": project_config,
        "defaults": defaults,
        "placement_member": application_member or "",
    }
    reserved_owner_key = "user.cloin.incus_compose.owner"
    configs = [project_config, _mapping(defaults.get("config"), "defaults.config")]
    configs.extend(service.get("config") or {} for service in normalized_services.values())
    configs.extend(network.get("config") or {} for network in normalized_networks.values())
    configs.extend(volume.get("config") or {} for volume in normalized_volumes.values())
    if any(reserved_owner_key in config for config in configs):
        raise AnsibleFilterError(
            f"application '{name}' cannot set reserved config key {reserved_owner_key}"
        )
    return {
        "application": application,
        "services": normalized_services,
        "networks": normalized_networks,
        "volumes": normalized_volumes,
    }


def incus_compose_normalize_catalog(definitions: list) -> list[dict]:
    if not isinstance(definitions, list):
        raise AnsibleFilterError("the application catalog must be a list")
    normalized = [incus_compose_normalize_application(item) for item in definitions]
    names = [item["application"]["name"] for item in normalized]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AnsibleFilterError(
            "application names are duplicated: " + ", ".join(duplicates)
        )
    return normalized


def _validate_cluster_network_placement(definitions: list[dict]) -> None:
    network_members: dict[str, set[str]] = {}
    network_types: dict[str, str] = {}
    for definition in definitions:
        application, services, networks = _application_context(definition)
        app_name = str(application["name"])
        defaults = application.get("defaults", {})
        app_member = str(application.get("placement_member", ""))
        for logical_name, network in networks.items():
            physical_name = incus_app_network_name(app_name, logical_name, network)
            network_types.setdefault(physical_name, str(network.get("type", "bridge")))
        for service in services.values():
            member = str(service.get("target") or app_member)
            if not member:
                continue
            for attachment in _service_networks(service, defaults):
                physical_name = _physical_network_name(
                    app_name, attachment["name"], networks
                )
                network_members.setdefault(physical_name, set()).add(member)
    for network_name, members in network_members.items():
        if network_types.get(network_name, "bridge") == "bridge" and len(members) > 1:
            raise AnsibleFilterError(
                f"bridge network '{network_name}' spans placement members: "
                f"{', '.join(sorted(members))}"
            )


def incus_compose_validate_catalog(definitions: list) -> bool:
    """Strictly validate public v1 manifests and catalog-wide invariants."""

    normalized = incus_compose_normalize_catalog(definitions)
    incus_app_validate_catalog(normalized)
    _validate_cluster_network_placement(normalized)
    return True


def incus_compose_service_order(services: Mapping) -> list[str]:
    """Order either public v1 or normalized internal service mappings."""

    if not isinstance(services, Mapping):
        raise AnsibleFilterError("application.services must be a mapping")
    if any(isinstance(value, Mapping) and "type" in value for value in services.values()):
        services = {
            str(name): _normalize_service_v1(str(name), value, None)
            for name, value in services.items()
        }
    return incus_app_service_order(services)


def incus_compose_route_count(definitions: list) -> int:
    return incus_app_ingress_count(incus_compose_normalize_catalog(definitions))


def incus_compose_catalog_order(definitions: list) -> list[dict]:
    """Place applications that own shared networks before their consumers."""

    normalized = incus_compose_normalize_catalog(definitions)
    incus_app_validate_catalog(normalized)
    owners: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    public_by_name: dict[str, dict] = {}

    for public, internal in zip(definitions, normalized, strict=True):
        application, _services, networks = _application_context(internal)
        app_name = str(application["name"])
        public_by_name[app_name] = public
        dependencies[app_name] = set()
        for logical_name, network in networks.items():
            if network.get("external", False):
                continue
            physical_name = incus_app_network_name(app_name, logical_name, network)
            previous = owners.get(physical_name)
            if previous and previous != app_name:
                raise AnsibleFilterError(
                    f"managed network '{physical_name}' is owned by both "
                    f"{previous} and {app_name}"
                )
            owners[physical_name] = app_name

    for internal in normalized:
        application, services, networks = _application_context(internal)
        app_name = str(application["name"])
        defaults = application.get("defaults", {})
        used_networks = {
            attachment["name"]
            for service in services.values()
            for attachment in _service_networks(service, defaults)
        }
        for logical_name in used_networks:
            network = networks[logical_name]
            if not network.get("external", False):
                continue
            physical_name = incus_app_network_name(app_name, logical_name, network)
            owner = owners.get(physical_name)
            if owner and owner != app_name:
                dependencies[app_name].add(owner)

    ordered: list[str] = []
    remaining = list(public_by_name)
    while remaining:
        ready = [
            name
            for name in remaining
            if dependencies[name].issubset(ordered)
        ]
        if not ready:
            raise AnsibleFilterError(
                "application network dependencies contain a cycle involving: "
                + ", ".join(remaining)
            )
        for name in ready:
            ordered.append(name)
            remaining.remove(name)
    return [public_by_name[name] for name in ordered]


def incus_compose_route_bindings(
    definitions: list, instances: list, strict: bool = True
) -> list[dict]:
    return incus_app_ingress_bindings(
        incus_compose_normalize_catalog(definitions), instances, strict
    )


def incus_compose_ownership_snapshot(
    definition: Mapping, storage_drivers: Mapping, clustered: bool,
    default_pool: str = "default",
) -> dict:
    """Keep only nonsecret identifiers needed to retire an application."""
    normalized = incus_compose_normalize_application(definition)
    app = normalized["application"]
    name, project = app["name"], app["project"]
    order = incus_app_service_order(normalized["services"])
    services = [
        {"name": normalized["services"][key].get("name", key)}
        for key in order
    ]
    networks = [
        {
            "name": incus_app_network_name(name, key, value),
            "project": value.get(
                "project", project if boolean(app["project_config"].get(
                    "features.networks", False), strict=False) else "default"
            ),
        }
        for key, value in normalized["networks"].items()
        if not value.get("external", False)
    ]
    volumes = []
    for key, value in normalized["volumes"].items():
        pool = value.get("pool", app["defaults"].get("storage_pool", default_pool))
        target = (
            value.get("target") or app["placement_member"]
            if clustered and storage_drivers.get(pool) in {"btrfs", "dir", "lvm", "zfs"}
            else ""
        )
        volumes.append({
            "name": value.get("name", key), "pool": pool,
            "target": target, "retain": boolean(value.get("retain", True), strict=False),
        })
    return {
        "name": name, "project": project, "services": services,
        "networks": networks, "volumes": volumes,
    }


def incus_compose_system_instance_matches(
    existing: Mapping, description: str, config: Mapping,
    devices: list, profiles: list, state: str,
) -> bool:
    """Ignore the ownership key when deciding whether damex must update a system instance."""
    expected_config = {
        str(key): _incus_string(value)
        for key, value in config.items()
        if key != "environment_variables" and value is not None
    }
    for item in config.get("environment_variables", []):
        expected_config[f"environment.{item['name']}"] = _incus_string(item["value"])
    current_config = {
        key: value for key, value in existing.get("config", {}).items()
        if not key.startswith(("volatile.", "image."))
        and key != "user.cloin.incus_compose.owner"
    }
    return (
        existing.get("description", "") == description
        and current_config == expected_config
        and existing.get("devices", {}) == incus_app_devices_config(devices)
        and existing.get("profiles", []) == profiles
        and existing.get("status") == ("Running" if state == "started" else "Stopped")
    )


def incus_compose_prune_plan(desired: list, inventory: list) -> list:
    """Reject ambiguous ownership before returning missing active apps."""
    if not desired:
        raise AnsibleFilterError("prune requires a nonempty desired catalog")
    normalized = incus_compose_normalize_catalog(desired)
    incus_app_validate_catalog(normalized)
    desired_names = {item["application"]["name"] for item in normalized}
    desired_projects = [item["application"]["project"] for item in normalized]
    if len(desired_projects) != len(set(desired_projects)):
        raise AnsibleFilterError("prune requires one application per project")
    if any(item["application"]["state"] != "present" for item in normalized):
        raise AnsibleFilterError("prune requires present-only desired definitions")
    if any(item.get("status") == "pending" and item["name"] not in desired_names
           for item in inventory):
        raise AnsibleFilterError(
            "prune found an incomplete application absent from the catalog; "
            "reconcile or inspect it before pruning"
        )
    active = [item for item in inventory if item.get("status") == "active"]
    projects = {}
    for item in active:
        previous = projects.setdefault(item["project"], item["name"])
        if previous != item["name"]:
            raise AnsibleFilterError("prune inventory contains a shared project")
    missing = [item for item in active if item["name"] not in desired_names]
    missing_projects = {item["project"] for item in missing}
    if missing_projects.intersection(desired_projects):
        raise AnsibleFilterError("a desired application reuses a retired project")
    retired_networks = {
        (network["project"], network["name"])
        for item in missing for network in item["networks"]
    }
    all_networks = {}
    for item in active:
        for network in item["networks"]:
            key = (network["project"], network["name"])
            previous = all_networks.setdefault(key, item["name"])
            if previous != item["name"]:
                raise AnsibleFilterError("prune inventory contains shared network ownership")
    for item in normalized:
        app = item["application"]
        for logical_name, network in item["networks"].items():
            network_project = network.get(
                "project", app["project"] if boolean(app["project_config"].get(
                    "features.networks", False), strict=False) else "default"
            )
            physical_name = incus_app_network_name(
                app["name"], logical_name, network
            )
            if (network_project, physical_name) in retired_networks:
                raise AnsibleFilterError("a desired application uses a retiring network")
    return missing


class FilterModule:
    """Expose the collection's stable filter surface to Ansible."""

    def filters(self):
        return {
            "is_ipv4_address": incus_app_is_ipv4_address,
            "is_private_ipv4_prefix": incus_app_is_private_ipv4_prefix,
            "service_order": incus_compose_service_order,
            "network_name": incus_app_network_name,
            "ownership_snapshot": incus_compose_ownership_snapshot,
            "system_instance_matches": incus_compose_system_instance_matches,
            "prune_plan": incus_compose_prune_plan,
            "network_attachment": incus_app_network_attachment,
            "validate_catalog": incus_compose_validate_catalog,
            "normalize_application": incus_compose_normalize_application,
            "normalize_catalog": incus_compose_normalize_catalog,
            "port_device": incus_app_exposure_device,
            "route_count": incus_compose_route_count,
            "route_bindings": incus_compose_route_bindings,
            "catalog_order": incus_compose_catalog_order,
            "oci_defaults": incus_app_oci_defaults,
            "oci_raw_config": incus_app_oci_raw_config,
            "devices_config": incus_app_devices_config,
        }
