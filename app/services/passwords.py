"""Password hashing (argon2id) and the password policy."""

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi's defaults are the RFC 9106 low-memory profile.
_hasher = PasswordHasher()
# Verifying against this makes "no such account" cost the same as "wrong password".
_DUMMY_HASH = _hasher.hash("this is not anyone's password")

# The most common passwords that also pass a length check. Not a substitute for a breach
# database, but it stops the obvious ones.
COMMON_PASSWORDS = frozenset(
    word
    for line in (
        "password password1 password12 password123 password1234 passw0rd p@ssw0rd",
        "p@ssword qwertyuiop qwerty123 qwerty1234 qwertyuiop1 1qaz2wsx 1q2w3e4r",
        "1q2w3e4r5t 1qazxsw2 iloveyou1 iloveyou12 iloveyou123 letmein123 welcome123",
        "welcome1234 admin12345 administrator abc123456 abcd123456 abcdefghij abcdefg123",
        "123456789 1234567890 12345678910 0123456789 0987654321 1111111111 0000000000",
        "1234512345 9876543210 987654321 123123123 123321123 changeme123 changeme1234",
        "trustno1234 monkey1234 dragon1234 football123 baseball123 superman123",
        "batman1234 princess123 sunshine123 whatever123 shadow1234 master1234",
        "michael1234 jennifer123 charlie1234 passwordpassword wardrobe123 wardrobe1234",
        "mypassword1 mypassword12 mypassword123 secret1234 secret12345 testtest12",
        "test123456 football1234 liverpool123 chelsea1234 arsenal1234 starwars123",
        "pokemon1234 hello12345 hellohello1 computer123 internet123 freedom1234",
        "letmein1234 login12345 access12345 zaq12wsxcde3 asdfghjkl1 asdfghjkl12",
        "asdf123456 zxcvbnm123 qazwsxedc123 q1w2e3r4t5 password!1 password1!",
        "password@123 pass1234567 pass12345678 123456a123 a123456789",
    )
    for word in line.split()
)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    """True if `password` matches. A missing hash still costs one verification."""
    try:
        _hasher.verify(hashed or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return hashed is not None


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def validate_password(password: str, email: str = "", min_length: int = 10) -> str | None:
    """None if acceptable, else a message the user can act on."""
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    if len(password) > 256:
        return "That password is too long (256 characters at most)."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or re.sub(r"[^a-z0-9]", "", lowered) in COMMON_PASSWORDS:
        return "That password is too common. Pick something less guessable."
    if len(set(password)) < 4:
        return "Use a mix of different characters."
    local = email.split("@", 1)[0].lower()
    if len(local) >= 4 and (local in lowered):
        return "Don't use your email address in your password."
    return None


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and EMAIL_RE.match(email) is not None
