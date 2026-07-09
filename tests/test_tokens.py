"""JWT session-token tests: round-trip, expiry, tampering, algorithm pinning."""

import unittest
from unittest.mock import patch

import jwt

from app import config, tokens
from app.tokens import InvalidTokenError


class TestTokens(unittest.TestCase):

    def test_roundtrip_returns_subject(self):
        t = tokens.create_access_token("12345")
        self.assertEqual(tokens.decode_token(t), "12345")

    def test_expired_token_rejected(self):
        with patch.object(config, "JWT_EXPIRE_HOURS", -1):   # exp in the past
            t = tokens.create_access_token("12345")
        with self.assertRaises(InvalidTokenError) as ctx:
            tokens.decode_token(t)
        self.assertIn("انتهت", str(ctx.exception))

    def test_tampered_signature_rejected(self):
        t = tokens.create_access_token("12345")
        with self.assertRaises(InvalidTokenError):
            tokens.decode_token(t + "x")

    def test_wrong_secret_rejected(self):
        forged = jwt.encode({"sub": "12345"}, "attacker-secret", algorithm="HS256")
        with self.assertRaises(InvalidTokenError):
            tokens.decode_token(forged)

    def test_alg_none_rejected(self):
        # classic JWT bypass attempt — must be refused (we pin HS256)
        forged = jwt.encode({"sub": "12345"}, key=None, algorithm="none")
        with self.assertRaises(InvalidTokenError):
            tokens.decode_token(forged)

    def test_missing_subject_rejected(self):
        t = jwt.encode({"foo": "bar"}, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
        with self.assertRaises(InvalidTokenError):
            tokens.decode_token(t)

    def test_token_without_exp_rejected(self):
        t = jwt.encode({"sub": "12345"}, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
        with self.assertRaises(InvalidTokenError):
            tokens.decode_token(t)  # require=['exp','sub'] → no exp is invalid


class TestSecretGuard(unittest.TestCase):
    """Fail closed: production must refuse a weak/default JWT secret."""

    def test_production_default_secret_aborts(self):
        with patch.object(config, "API_ENV", "production"), \
             patch.object(config, "JWT_SECRET", "dev-insecure-change-me"):
            with self.assertRaises(RuntimeError):
                config.assert_secure_for_production()

    def test_production_short_secret_aborts(self):
        with patch.object(config, "API_ENV", "production"), \
             patch.object(config, "JWT_SECRET", "tooshort"):
            with self.assertRaises(RuntimeError):
                config.assert_secure_for_production()

    def test_production_strong_secret_ok(self):
        with patch.object(config, "API_ENV", "production"), \
             patch.object(config, "JWT_SECRET", "x" * 40):
            config.assert_secure_for_production()  # no raise

    def test_development_default_secret_allowed(self):
        with patch.object(config, "API_ENV", "development"):
            config.assert_secure_for_production()  # dev keeps the default


if __name__ == "__main__":
    unittest.main()
