"""The in-memory, per-session credential cache.

This is the only place a plaintext secret lives outside the vault and the
connect dialog, and it exists for exactly one reason: **a dedicated SSH
connection authenticates separately.** If the user typed a one-off password for
an SFTP pane and then opens an SSH terminal to the same profile, a second
prompt is the failure mode this plan exists to avoid — so the typed secret is
remembered for the rest of the session.

Its lifetime is explicit and short, matching what §7 of the plan demands:

* cleared on vault lock (a locked vault must mean "forget everything"),
* cleared on profile disconnect,
* cleared at exit,
* never written to disk (that is the whole distinction from "Save to vault"),
* never logged — ``Credential.__repr__`` is already redacted.
"""

from __future__ import annotations

from surftp.net.types import ConnectionProfile, Credential


class CredentialCache:
    """Memory-only secrets keyed by profile, opt-in per connection."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[tuple[object, ...], Credential] = {}

    @staticmethod
    def key_for(profile: ConnectionProfile) -> tuple[object, ...]:
        """The cache key for a profile: its vault id when saved, else a stable address.

        Two unsaved profiles pointing at one server must share a cache entry,
        because a one-off connection typed by hand is the very case the cache
        exists for.
        """
        if profile.id is not None:
            return ("profile", profile.id)
        return ("temp", profile.host, profile.port, profile.username, profile.auth_method)

    def get(self, profile: ConnectionProfile) -> Credential | None:
        """Return the cached credential for ``profile``, or ``None``."""
        return self._entries.get(self.key_for(profile))

    def put(self, profile: ConnectionProfile, credential: Credential) -> None:
        """Remember ``credential`` for ``profile`` for this session.

        Empty credentials (the agent case) are not worth caching; storing them
        would only mask a future, real prompt.
        """
        if credential.password or credential.key_passphrase or credential.key_passphrases:
            self._entries[self.key_for(profile)] = credential

    def drop(self, profile: ConnectionProfile) -> None:
        """Forget one profile's cached secret (on disconnect)."""
        self._entries.pop(self.key_for(profile), None)

    def clear(self) -> None:
        """Forget everything (on vault lock and at exit)."""
        self._entries.clear()

    def __len__(self) -> int:
        """Number of cached entries, for tests and diagnostics."""
        return len(self._entries)


_default_cache: CredentialCache | None = None


def get_credential_cache() -> CredentialCache:
    """Return the process-wide cache instance."""
    global _default_cache
    if _default_cache is None:
        _default_cache = CredentialCache()
    return _default_cache