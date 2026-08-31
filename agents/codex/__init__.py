"""Direct Codex app-server integration for the local bridge.

This package speaks the official stdio JSONL app-server protocol directly. It
does not implement DSH's native agent/provider loop and never handles API
keys, cookies, or ChatGPT auth tokens.
"""

from .app_server_client import (
    AppServerClient,
    AppServerConfig,
    CodexAmbiguousRequestError,
    JsonRpcError,
)
from .auth import CodexAuthService
from .cancellation import CancellationToken
from .external_auth import ChatgptSubscriptionBroker, managed_auth_file, prepare_isolated_home
from .provider import CodexAgentService, CodexBusyError
from .thread_manager import ThreadManager, ThreadMappingStore
from .types import (
    AgentEvent,
    AgentEventType,
    CodexCompatibilityError,
    CodexError,
    CodexProcessError,
    CodexTimeoutError,
    LoginStatus,
)

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AppServerClient",
    "AppServerConfig",
    "CancellationToken",
    "ChatgptSubscriptionBroker",
    "CodexAgentService",
    "CodexBusyError",
    "CodexAuthService",
    "CodexAmbiguousRequestError",
    "CodexCompatibilityError",
    "CodexError",
    "CodexProcessError",
    "CodexTimeoutError",
    "JsonRpcError",
    "LoginStatus",
    "managed_auth_file",
    "prepare_isolated_home",
    "ThreadManager",
    "ThreadMappingStore",
]
