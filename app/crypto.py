import os
import base64
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet
from app.config import settings, BASE_DIR

def _resolve_key_file() -> Path:
    """Determine path for .secret_key, prioritizing persistent data directory."""
    data_dir = settings.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    target_key = data_dir / ".secret_key"
    legacy_key = BASE_DIR / ".secret_key"

    if target_key.exists():
        return target_key

    # Migrate key from BASE_DIR if it already exists there
    if legacy_key.exists():
        try:
            target_key.write_bytes(legacy_key.read_bytes())
            try:
                target_key.chmod(0o600)
            except Exception:
                pass
            return target_key
        except Exception:
            return legacy_key

    return target_key


KEY_FILE = _resolve_key_file()


def _get_or_create_key() -> bytes:
    """Retrieve or generate a 32-byte urlsafe base64 key for Fernet."""
    if hasattr(settings, "secret_key") and settings.secret_key:
        # Derive 32-byte key from custom secret_key
        digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    if KEY_FILE.exists():
        try:
            return KEY_FILE.read_bytes().strip()
        except Exception:
            pass

    # Generate new key and persist locally
    new_key = Fernet.generate_key()
    try:
        KEY_FILE.write_bytes(new_key)
        try:
            KEY_FILE.chmod(0o600)
        except Exception:
            pass
    except Exception:
        pass
    return new_key


_cipher = Fernet(_get_or_create_key())


def encrypt_secret(val: str | None) -> str | None:
    """Encrypt a secret string. Returns urlsafe base64 string."""
    if not val:
        return val
    # If already encrypted, return as is
    if val.startswith("gAAAAA"):
        return val
    return _cipher.encrypt(val.encode("utf-8")).decode("utf-8")


def decrypt_secret(val: str | None) -> str | None:
    """Decrypt a secret string. Fallbacks to plaintext if not encrypted."""
    if not val:
        return val
    if not val.startswith("gAAAAA"):
        return val  # Unencrypted legacy value
    try:
        return _cipher.decrypt(val.encode("utf-8")).decode("utf-8")
    except Exception:
        # Key mismatch or corrupt data, return original
        return val
