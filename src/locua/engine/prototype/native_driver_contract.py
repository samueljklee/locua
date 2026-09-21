"""Explicit contract pin for LocuaDriver's same-handle native editor facts."""
CONTRACT_ID = "locua.native.editor.v1"
# Filled from the reviewed producer before a real runtime is accepted.
SOURCE_FINGERPRINT = "37e78898aa711c93658d20f5371fcf3acab308a09cedbcfdc27d09ab6296aad9"


def check_native_editor_contract(value):
    return (isinstance(value, dict) and value.get("id") == CONTRACT_ID
            and value.get("source_fingerprint_sha256") == SOURCE_FINGERPRINT
            and value.get("platform") == "macos" and value.get("plane") == "editor_buffer"
            and value.get("handle_binding") == "same_snapshot_element_token")
