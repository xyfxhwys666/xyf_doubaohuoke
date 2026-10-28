from cryptography.fernet import Fernet


class CredentialCipher:
    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_base64_key(cls, key: str) -> "CredentialCipher":
        return cls(Fernet(key.encode("ascii")))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")

    def mask(self, plaintext: str) -> str:
        if len(plaintext) <= 8:
            return "*" * len(plaintext)
        return f"{plaintext[:3]}{'*' * 8}{plaintext[-4:]}"
