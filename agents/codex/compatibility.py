"""Pinned stable app-server compatibility gate.

The repository intentionally supports one audited Codex CLI/schema build.  The
runtime can be relaxed for an explicitly configured fake server in tests by
passing ``expected_cli_version=None``; production defaults remain fail-closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .types import CodexCompatibilityError


def _load_manifest() -> Mapping[str, Any]:
    path = Path(__file__).with_name("protocol-manifest.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex protocol manifest is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("Codex protocol manifest is invalid")
    return value


STABLE_PROTOCOL_MANIFEST: Mapping[str, Any] = _load_manifest()
EXPECTED_CLI_VERSION = str(STABLE_PROTOCOL_MANIFEST["codexCliVersion"])
STABLE_SCHEMA_BUNDLE_SHA256 = str(STABLE_PROTOCOL_MANIFEST["schemaSha256"])
STABLE_EXPERIMENTAL_API = bool(STABLE_PROTOCOL_MANIFEST.get("experimentalApi", False))
STABLE_REQUEST_ATTESTATION = bool(STABLE_PROTOCOL_MANIFEST.get("requestAttestation", False))

_VERSION_PATTERN = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?"
_TERMINAL_TOKEN_PATTERN = r"[0-9A-Za-z._/-]{1,128}"


@dataclass(frozen=True)
class ProtocolInfo:
    """Safe, bounded compatibility metadata; never includes paths or account data."""

    user_agent: str | None
    cli_version: str | None
    schema_sha256: str = STABLE_SCHEMA_BUNDLE_SHA256


class ProtocolCompatibilityGate:
    """Validate the initialize result against the audited CLI version."""

    def __init__(
        self,
        expected_cli_version: str | None = EXPECTED_CLI_VERSION,
        *,
        client_name: str = "xiaoman-dsh",
        client_version: str = "0.1.0",
    ) -> None:
        self.expected_cli_version = expected_cli_version
        self.client_name = client_name
        self.client_version = client_version

    def validate_initialize(self, result: Mapping[str, Any]) -> ProtocolInfo:
        raw_user_agent = result.get("userAgent")
        user_agent = raw_user_agent if isinstance(raw_user_agent, str) else None
        expected = self.expected_cli_version
        cli_version = _extract_version(
            user_agent,
            client_name=self.client_name,
            client_version=self.client_version,
        )
        if expected is not None and cli_version != expected:
            raise CodexCompatibilityError(
                "unsupported Codex app-server CLI version",
                code="codex_version_unsupported",
            )
        return ProtocolInfo(user_agent=user_agent, cli_version=cli_version)


def _extract_version(
    user_agent: str | None,
    *,
    client_name: str,
    client_version: str,
) -> str | None:
    """Parse only the exact app-server UA grammar emitted after initialize.

    Codex constructs this value as ``originator/version (OS; arch) terminal
    (client; version)``.  The originator and suffix both come from the fixed
    ``clientInfo`` we send.  Anchoring the entire value prevents an arbitrary
    banner containing the pinned version as a substring from passing the
    runtime compatibility gate.
    """

    if (
        not user_agent
        or len(user_agent) > 512
        or not user_agent.isascii()
        or any(not 0x20 <= ord(character) <= 0x7E for character in user_agent)
    ):
        return None
    client = re.escape(client_name)
    client_release = re.escape(client_version)
    match = re.fullmatch(
        rf"{client}/(?P<version>{_VERSION_PATTERN}) "
        rf"\([^();\r\n]{{1,160}}; [^();\r\n]{{1,64}}\) "
        rf"{_TERMINAL_TOKEN_PATTERN} "
        rf"\({client}; {client_release}\)",
        user_agent,
    )
    return match.group("version") if match else None
