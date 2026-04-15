"""Tests for JSON-Schema validation of customer profiles."""

import json

import pytest


def _write_profile(dir_path, content) -> str:
    path = dir_path / "profile.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


@pytest.fixture
def server_mod(monkeypatch, tmp_path):
    # Force a clean profile cache each test
    import server

    monkeypatch.setattr(server, "_CUSTOMER_PROFILE", None)
    return server


def test_valid_profile_passes(server_mod, tmp_path):
    path = _write_profile(
        tmp_path,
        {
            "$schema": "../profile.schema.json",
            "jira": {
                "project_overrides": {
                    "ABC": {
                        "issue_type_overrides": {
                            "Story": {
                                "required_fields": ["customfield_1"],
                                "default_fields": {"customfield_1": [{"key": "X-1"}]},
                            }
                        }
                    }
                }
            },
        },
    )
    with open(path) as f:
        profile = json.load(f)
    server_mod._validate_customer_profile(profile, path)  # no raise


def test_unknown_top_level_key_fails(server_mod, tmp_path):
    profile = {"jira": {}, "unknown_root_key": 1}
    with pytest.raises(RuntimeError, match="unknown_root_key"):
        server_mod._validate_customer_profile(profile, "<test>")


def test_typo_default_fiedls_fails(server_mod, tmp_path):
    profile = {
        "jira": {
            "project_overrides": {
                "ABC": {
                    "issue_type_overrides": {
                        "Story": {"default_fiedls": {}}  # typo
                    }
                }
            }
        }
    }
    with pytest.raises(RuntimeError, match="default_fiedls"):
        server_mod._validate_customer_profile(profile, "<test>")


def test_required_fields_wrong_type_fails(server_mod):
    profile = {
        "jira": {
            "project_overrides": {
                "ABC": {
                    "issue_type_overrides": {
                        "Story": {"required_fields": "not-an-array"}
                    }
                }
            }
        }
    }
    with pytest.raises(RuntimeError):
        server_mod._validate_customer_profile(profile, "<test>")


def test_required_fields_duplicates_fail(server_mod):
    profile = {
        "jira": {
            "project_overrides": {
                "ABC": {
                    "issue_type_overrides": {
                        "Story": {"required_fields": ["a", "a"]}
                    }
                }
            }
        }
    }
    with pytest.raises(RuntimeError):
        server_mod._validate_customer_profile(profile, "<test>")


def test_empty_profile_passes(server_mod):
    server_mod._validate_customer_profile({}, "<test>")
    server_mod._validate_customer_profile({"jira": {}}, "<test>")


def test_validation_skipped_when_jsonschema_missing(server_mod, monkeypatch):
    """If the jsonschema package isn't importable we skip silently."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Would normally fail validation; should now pass due to missing package
    server_mod._validate_customer_profile({"bogus": 1}, "<test>")
