from secrets import token_hex

from cryptography.fernet import Fernet

from app.security.credentials import CredentialCipher


def test_credential_cipher_round_trip_does_not_expose_plaintext() -> None:
    cipher = CredentialCipher.from_base64_key(Fernet.generate_key().decode("ascii"))
    plaintext = f"sk-{token_hex(6)}-value"
    encrypted = cipher.encrypt(plaintext)
    assert plaintext not in encrypted
    assert cipher.decrypt(encrypted) == plaintext
    assert cipher.mask(plaintext) == "sk-********alue"
