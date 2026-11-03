"""Small, explicit shared-token boundary for the course prototype.

This is service-wide RBAC, not user authentication or tenant isolation. Local
unauthenticated mode is deliberately restricted to loopback transport peers.
Never put this mode behind a reverse proxy or expose it as a shared service.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AccessPolicy:
    viewer_token: str = ""
    editor_token: str = ""

    def __post_init__(self) -> None:
        for token in (self.viewer_token, self.editor_token):
            if token and (len(token) < 24 or not token.isascii() or token.strip() != token):
                raise ValueError("Configured tokens must be ASCII, at least 24 characters, with no surrounding whitespace")
        if self.viewer_token and self.editor_token and hmac.compare_digest(self.viewer_token, self.editor_token):
            raise ValueError("Viewer and editor tokens must be different")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AccessPolicy:
        env = os.environ if environ is None else environ
        policy = cls(
            viewer_token=env.get("FABRIC_VIEWER_TOKEN", ""),
            editor_token=env.get("FABRIC_EDITOR_TOKEN", ""),
        )
        if policy.mode == "local_demo" and env.get("FABRIC_SANDBOX_ENV", "local") != "local":
            raise ValueError("Configure a FABRIC_*_TOKEN outside the local demonstration environment")
        return policy

    @property
    def mode(self) -> str:
        return "bearer_roles" if self.viewer_token or self.editor_token else "local_demo"

    def identify(self, authorization: str | None, peer: str | None) -> str | None:
        if self.mode == "local_demo":
            # Starlette's in-process TestClient has no network transport and uses
            # this sentinel. This is never read from the request's Host header.
            if peer == "testclient":
                return "editor"
            try:
                if peer and ipaddress.ip_address(peer).is_loopback:
                    return "editor"
            except ValueError:
                pass
            return None
        if not authorization:
            return None
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or not token.isascii():
            return None
        if self.editor_token and hmac.compare_digest(token, self.editor_token):
            return "editor"
        if self.viewer_token and hmac.compare_digest(token, self.viewer_token):
            return "viewer"
        return None

    @staticmethod
    def local_context_is_safe(host: str, origin: str | None, forwarded: bool) -> bool:
        # Do not let proxy metadata or a DNS-rebinding Host turn the loopback
        # demonstration server into an unauthenticated remote service.
        if forwarded:
            return False
        try:
            parsed_host = urlsplit("//" + host)
            hostname = parsed_host.hostname
            if hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
                return False
            if parsed_host.username or parsed_host.password or parsed_host.path:
                return False
            if origin:
                parsed_origin = urlsplit(origin)
                if parsed_origin.scheme not in {"http", "https"}:
                    return False
                if parsed_origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    return False
                if parsed_origin.username or parsed_origin.password:
                    return False
                if parsed_origin.port not in {8000, 5173}:
                    return False
        except ValueError:
            return False
        return True

    @staticmethod
    def requires_editor(method: str, path: str) -> bool:
        normalized = path.rstrip("/")
        read_only_explain = normalized == "/api/assistant/explain" or (
            normalized.startswith("/api/v2/simulations/") and normalized.endswith("/explain")
        )
        return method not in {"GET", "HEAD", "OPTIONS"} and not read_only_explain
