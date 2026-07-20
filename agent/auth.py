import hashlib
import json
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent.profiler import validate_student_id

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "../data/accounts.json")

_bearer_scheme = HTTPBearer(auto_error=False)

# Cached registry of {api_key_sha256: Account}, built lazily on first use.
_registry: dict[str, "Account"] | None = None


@dataclass(frozen=True)
class Account:
    account_id: str
    role: str
    student_ids: frozenset[str]


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _load_registry() -> dict[str, Account]:
    global _registry
    if _registry is None:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            accounts = json.load(f)
        _registry = {
            entry["api_key_sha256"]: Account(
                account_id=entry["account_id"],
                role=entry["role"],
                student_ids=frozenset(entry["student_ids"]),
            )
            for entry in accounts
        }
    return _registry


def reset_registry():
    """Drop the cached registry so the next request reloads it. For tests."""
    global _registry
    _registry = None


def require_account(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Account:
    """Resolve the bearer key to an account, or reject with 401."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    account = _load_registry().get(_hash_key(credentials.credentials))
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return account


def authorize_student(account: Account, student_id: str):
    """Ensure the account is allowed to act on this student, or reject with 403.

    The ID is validated first so malformed IDs still surface as 400, matching
    the behavior of the underlying profile storage.
    """
    validate_student_id(student_id)
    if student_id not in account.student_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this student.",
        )
