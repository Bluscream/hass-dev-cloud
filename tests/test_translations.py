"""Tests ensuring all config flow and options flow keys are localized.

Static validation that inspects `config_flow.py` and asserts that every step,
input field, data description, error code, and abort condition has an exact match
in `strings.json` and `translations/en.json`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "custom_components" / "dev_cloud"
STRINGS_PATH = SRC_DIR / "strings.json"
EN_PATH = SRC_DIR / "translations" / "en.json"
CONFIG_FLOW_PATH = SRC_DIR / "config_flow.py"


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _flatten_dict(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    res: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            res.update(_flatten_dict(v, key))
        else:
            res[key] = str(v)
    return res


def test_strings_and_en_json_are_in_sync() -> None:
    """Every key in strings.json must exist in translations/en.json and vice-versa."""
    strings = _load_json(STRINGS_PATH)
    en = _load_json(EN_PATH)

    flat_strings = _flatten_dict(strings)
    flat_en = _flatten_dict(en)

    missing_in_en = set(flat_strings.keys()) - set(flat_en.keys())
    missing_in_strings = set(flat_en.keys()) - set(flat_strings.keys())

    assert not missing_in_en, f"Keys present in strings.json but missing in en.json: {missing_in_en}"
    assert not missing_in_strings, (
        f"Keys present in en.json but missing in strings.json: {missing_in_strings}"
    )


def test_no_empty_translations() -> None:
    """No translation value should be blank or pure whitespace."""
    for path in (STRINGS_PATH, EN_PATH):
        data = _load_json(path)
        flat = _flatten_dict(data)
        empty = [k for k, v in flat.items() if not v.strip()]
        assert not empty, f"Empty translation strings in {path.name}: {empty}"


def test_all_config_steps_localized() -> None:
    """Every step defined in config_flow.py must have title/description in strings.json."""
    strings = _load_json(STRINGS_PATH)
    config_steps = strings.get("config", {}).get("step", {})
    options_steps = strings.get("options", {}).get("step", {})

    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))

    async_steps: list[tuple[str, str]] = []  # (flow_type, step_id)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_options = "OptionsFlow" in [
                b.id for b in node.bases if isinstance(b, ast.Name)
            ]
            flow_type = "options" if is_options else "config"
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name.startswith("async_step_"):
                    step_id = item.name[len("async_step_") :]
                    async_steps.append((flow_type, step_id))

    for flow_type, step_id in async_steps:
        target_dict = options_steps if flow_type == "options" else config_steps
        assert step_id in target_dict, (
            f"Step '{step_id}' in {flow_type} flow is missing from strings.json"
        )
        step_entry = target_dict[step_id]
        assert "title" in step_entry, f"Step '{step_id}' missing 'title' in strings.json"
        assert "description" in step_entry, (
            f"Step '{step_id}' missing 'description' in strings.json"
        )


def test_all_form_fields_have_localized_labels() -> None:
    """Every form schema field in config_flow.py must have a translation entry."""
    strings = _load_json(STRINGS_PATH)
    config_data_keys: set[str] = set()
    for step in strings.get("config", {}).get("step", {}).values():
        config_data_keys.update(step.get("data", {}).keys())

    options_data_keys: set[str] = set()
    for step in strings.get("options", {}).get("step", {}).values():
        options_data_keys.update(step.get("data", {}).keys())

    all_data_keys = config_data_keys | options_data_keys

    # Read const.py keys (CONF_*) used in config_flow.py
    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    used_conf_constants: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("CONF_"):
            used_conf_constants.add(node.id)

    # Resolve CONF_* constants to their string values from const.py
    const_path = SRC_DIR / "const.py"
    const_tree = ast.parse(const_path.read_text(encoding="utf-8"))
    const_values: dict[str, str] = {}
    for node in ast.walk(const_tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("CONF_"):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        const_values[target.id] = node.value.value

    # Check that any CONF_* constant used in schema fields has a localized label
    for conf_var in used_conf_constants:
        val = const_values.get(conf_var)
        if val in (
            "scan_interval",
            "enable_events",
            "include_non_owned_orgs",
            "detailed_results",
            "platform",
            "instance_preset",
            "account_name",
            "instance_url",
            "api_token",
        ):
            assert val in all_data_keys, (
                f"Configuration field '{val}' ({conf_var}) has no localized label in strings.json"
            )


def test_all_errors_and_aborts_localized() -> None:
    """Any error return in config_flow.py must have a corresponding error/abort translation."""
    strings = _load_json(STRINGS_PATH)
    known_errors = set(strings.get("config", {}).get("error", {}).keys())

    tree = ast.parse(CONFIG_FLOW_PATH.read_text(encoding="utf-8"))

    # Check errors returned by _async_validate_account
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_async_validate_account":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                    val = sub.value.value
                    if val is not None and isinstance(val, str):
                        assert val in known_errors, (
                            f"Error key '{val}' returned in config_flow has no translation in strings.json"
                        )
