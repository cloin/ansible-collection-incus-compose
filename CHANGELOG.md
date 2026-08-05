# Changelog

## 0.1.1

- Prevent rendered application secrets from appearing in Ansible task output
  while catalogs and services are loaded, normalized, validated, inspected, and
  published.

## 0.1.0

- Add strict `cloin.incus-compose/v1` application manifests.
- Reconcile Incus projects, networks, volumes, OCI containers, system
  containers, and virtual machines.
- Resolve provider-neutral HTTP routes and explicit TCP/UDP ports.
- Add explicit Incus cluster member placement, two-phase managed network
  creation, and storage-driver-aware custom volume placement.
