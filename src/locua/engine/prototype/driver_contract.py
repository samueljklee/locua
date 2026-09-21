"""Explicit experimental driver pin; package version alone is insufficient."""
CONTRACT_ID = "locua.browser.semantic.v4"
UPSTREAM = "9e60d90b8681d3ba7ccf2c7801dbaa21b0d6efbb"
SOURCE_FINGERPRINT = "499107cec69475763f8fcf48875bb63a57b5bd8e4d2f03661c51aeb8a552594c"
BINARY_SHA256 = "8cf91dc4f80cc8fcd8f6d29f2ed0fa9f1eaa9bfb5c11ee21414518cde60815f9"


def check_contract(contract):
    return (contract.get("id") == CONTRACT_ID and contract.get("upstream_revision") == UPSTREAM
            and contract.get("source_fingerprint_sha256") == SOURCE_FINGERPRINT
            and contract.get("experimental_patch") is True and contract.get("atomic_capture") is False
            and contract.get("ancestry") == "frame_scoped_ax_parent"
            and contract.get("exact_value_source") == "DOMSnapshot")
