"""Шифрование секретов подключений (Fernet)."""

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, plain: str) -> bytes:
        return self._fernet.encrypt(plain.encode())

    def open(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken:
            raise ValueError("секрет не расшифровать") from None
