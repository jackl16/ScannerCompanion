"""Feature unlock codes, stored as SHA-256 hashes rather than plain text."""
import hashlib

FEATURE_CODE_HASHES = {
    "drive_photos": "691d7fbd8aa87fd10aec6dbdc4e6362c326b1e91f35e21eb9318448e432719dd",
}

def check_feature_code(entered: str) -> str | None:
    digest = hashlib.sha256(entered.strip().encode("utf-8")).hexdigest()
    for feature, expected in FEATURE_CODE_HASHES.items():
        if digest == expected:
            return feature
    return None