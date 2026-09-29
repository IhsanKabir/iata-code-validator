"""The tab password is checked against a salted fingerprint only."""
from src import tab_lock

SALT = b"fedcba9876543210"


def test_the_right_password_matches_its_fingerprint():
    digest = tab_lock.derive("a-test-password", SALT)
    assert tab_lock.check("a-test-password", SALT, digest)


def test_a_wrong_or_empty_password_does_not():
    digest = tab_lock.derive("a-test-password", SALT)
    assert not tab_lock.check("a-test-passwor", SALT, digest)
    assert not tab_lock.check("", SALT, digest)
    assert not tab_lock.check("wrong")          # against the real fingerprint


def test_the_fingerprint_is_salted_and_slow_to_guess():
    assert len(tab_lock.FFP_SALT) == 16 and len(tab_lock.FFP_HASH) == 32
    assert tab_lock.ITERATIONS >= 100_000
