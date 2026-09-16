from __future__ import annotations

from dataclasses import dataclass
import os


class UnsafeModeError(RuntimeError):
    """Raised before any provider object can be configured for live use."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    schema: str
    fernet_key: str
    mode: str = "simulation"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("VELMORA_MODE", "simulation")
        if mode != "simulation":
            raise UnsafeModeError("This milestone is simulation-only; live mode is not constructible")
        key = os.environ["FERNET_KEY"]
        return cls(os.environ["DATABASE_URL"], os.getenv("DATABASE_SCHEMA", "velmora"), key, mode)


def require_simulation(mode: str) -> None:
    if mode != "simulation":
        raise UnsafeModeError("Live suppliers and messengers are intentionally unavailable")
