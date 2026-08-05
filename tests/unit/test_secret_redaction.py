from __future__ import annotations

import pathlib

import yaml


COLLECTION_ROOT = pathlib.Path(__file__).parents[2]


def _tasks(relative_path: str) -> list[dict]:
    with (COLLECTION_ROOT / relative_path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _task_named(relative_path: str, name: str) -> dict:
    return next(task for task in _tasks(relative_path) if task["name"] == name)


def test_application_definitions_are_redacted_during_validation():
    path = "roles/application/tasks/validate.yml"
    expected = [
        "Validate the public application schema",
        "Normalize public inputs for reconciliation",
        "Normalize application values",
        "Validate application state and placement",
        "Validate per-service member placement",
        "Calculate dependency order",
    ]

    for name in expected:
        assert _task_named(path, name)["no_log"]


def test_service_definitions_and_live_configs_are_redacted():
    path = "roles/application/tasks/service.yml"
    expected_prefixes = [
        "Normalize service ",
        "Validate service ",
        "Calculate service ",
        "Inspect service ",
        "Decide whether service ",
        "Seed OCI image defaults ",
        "Inspect seeded OCI defaults ",
        "Ensure system service ",
    ]
    tasks = _tasks(path)

    for prefix in expected_prefixes:
        task = next(item for item in tasks if item["name"].startswith(prefix))
        assert task["no_log"]


def test_catalog_definitions_and_results_are_redacted():
    expected = {
        "roles/catalog/tasks/load_file.yml": [
            "Load ",
            "Add ",
        ],
        "roles/catalog/tasks/main.yml": [
            "Select and order the application catalog",
            "Validate the complete application catalog",
            "Reconcile applications in dependency order",
            "Query live Incus instances for routes",
            "Publish catalog reconciliation result",
        ],
    }

    for path, names in expected.items():
        tasks = _tasks(path)
        for name in names:
            task = next(item for item in tasks if item["name"].startswith(name))
            assert task["no_log"]
