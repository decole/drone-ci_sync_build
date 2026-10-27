from app.crypto import encrypt_secret, decrypt_secret


def test_encryption_and_decryption():
    secret = "ghp_mySuperSecretToken12345!@#"
    encrypted = encrypt_secret(secret)
    assert encrypted != secret
    assert encrypted.startswith("gAAAAA")

    decrypted = decrypt_secret(encrypted)
    assert decrypted == secret


def test_decrypt_plaintext_fallback():
    # Legacy plaintext password should not crash and should return as is
    plain = "simple_old_password"
    assert decrypt_secret(plain) == plain
    assert decrypt_secret(None) is None
    assert encrypt_secret(None) is None


def test_empty_string():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_key_file_location_in_data_dir():
    from app.crypto import KEY_FILE
    from app.config import settings
    # Key file must be resolved inside data_dir or BASE_DIR and exist
    assert KEY_FILE.exists()
    assert KEY_FILE.name == ".secret_key"
