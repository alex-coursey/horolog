
import os
import time

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier


class OIDCTokenVerifier(TokenVerifier):
    """Validate RS256 OIDC access tokens for one configured subject."""

    def __init__(self):
        self.issuer = os.environ["HOROLOG_OAUTH_ISSUER"]
        self.client_id = os.environ["HOROLOG_OAUTH_CLIENT_ID"]
        self.audience = os.getenv("HOROLOG_OAUTH_AUDIENCE", os.environ["HOROLOG_MCP_RESOURCE_URL"])
        self.allowed_subject = os.environ["HOROLOG_OAUTH_SUBJECT"]

        if not self.allowed_subject.strip():
            raise ValueError("Authorized OAuth subject is required")

        self.jwks = PyJWKClient(
            os.environ["HOROLOG_OAUTH_JWKS_URL"],
            cache_jwk_set=True,
            lifespan=300,
        )

    async def verify_token(
        self,
        token: str,
    ) -> AccessToken | None:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token)

            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "exp",
                        "sub",
                        "iat",
                    ]
                },
                leeway=30,
            )

            if claims["sub"] != self.allowed_subject:
                return None

            scopes = claims.get("scope", "")
            if isinstance(scopes, str):
                scopes = scopes.split()
            elif not isinstance(scopes, list):
                return None

            if not all(isinstance(s, str) for s in scopes):
                return None

            return AccessToken(
                token=token,
                client_id=self.client_id,
                scopes=scopes,
                expires_at=int(claims["exp"]),
                subject=claims["sub"],
                claims=claims,
            )

        except (
            jwt.PyJWTError,
            jwt.PyJWKClientError,
            ValueError,
            KeyError,
            TimeoutError,
            OSError,
        ):
            return None
