import pytest
from cryptography.fernet import Fernet

from hub.crypto import SecretBox


def test_secretbox_roundtrip():
    box = SecretBox(Fernet.generate_key().decode())
    token = box.seal("https://example.com/cal.ics?key=abc")
    assert isinstance(token, bytes)
    assert b"example.com" not in token
    assert box.open(token) == "https://example.com/cal.ics?key=abc"


def test_secretbox_wrong_key_raises_valueerror():
    token = SecretBox(Fernet.generate_key().decode()).seal("секрет")
    with pytest.raises(ValueError, match="секрет не расшифровать"):
        SecretBox(Fernet.generate_key().decode()).open(token)
