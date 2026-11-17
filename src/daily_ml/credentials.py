"""Persist one protected ChatGPT registration and its active token set."""

import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .locking import LockBusy, file_lock

PLAN_SCOPE = "chatgpt.tokens.use.direct"
MAX_RECORD_BYTES = 256 * 1024
CREDENTIAL_LOCK_WAIT = 20
CLIENT_ID = re.compile(r"[A-Za-z0-9._~-]{1,256}")
Text = Annotated[str, Field(min_length=1, max_length=1024)]
Token = Annotated[str, Field(min_length=1, max_length=32_768, pattern=r"^[!-~]+$")]


class StorageError(RuntimeError):
    """Protected storage could not be read, locked, or changed."""


class Account(BaseModel):
    """Verified OpenID identity and its issued workspace-bound client."""

    model_config = ConfigDict(strict=True, frozen=True)
    client_id: Text
    subject: Text
    email: Text


class Tokens(BaseModel):
    """Validated tokens, granted permissions and the access expiry timestamp."""

    model_config = ConfigDict(strict=True, frozen=True, allow_inf_nan=False)
    access_token: Token = Field(repr=False)
    refresh_token: Token = Field(repr=False)
    id_token: Token = Field(repr=False)
    scope: Annotated[str, Field(max_length=1024)]
    expires_at: float


class Session(BaseModel):
    """Atomic snapshot of account, credentials and sign-in generation."""

    model_config = ConfigDict(strict=True, frozen=True)
    generation: UUID = UUID(int=0)
    account: Account | None = None
    tokens: Tokens | None = Field(default=None, repr=False)
    reconnect: bool = False
    requires_consent: bool = False
    remote_revocation_unconfirmed: bool = False


class InstallationIdentity(BaseModel):
    """Independent stable identity for OpenAI's agent host."""

    model_config = ConfigDict(strict=True, frozen=True)
    host_id: UUID


class CredentialStore:
    """Manage owner-only files and serialize credential mutations across processes."""

    def __init__(self, data_dir: Path) -> None:
        self.directory = data_dir / "credentials"
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = self.directory.lstat()
            if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
                raise StorageError("Daily ML credential storage must be a directory you own.")
            self.directory.chmod(0o700)
        except OSError:
            raise StorageError("Could not protect Daily ML credential storage.") from None

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize a read, optional provider call and replacement.

        Yields
        ------
        None
            While the credential lock is held.

        Raises
        ------
        StorageError
            If the bounded lock wait ends or the lock is inaccessible.
        """
        try:
            with file_lock(self.directory / ".lock", wait=CREDENTIAL_LOCK_WAIT):
                yield
        except (OSError, LockBusy):
            raise StorageError("Daily ML credential storage is busy or unavailable. Try again.") from None

    def _read(self, name: str, model: type[BaseModel]) -> BaseModel | None:
        """Read a complete owner-only regular file, without following symlinks."""
        try:
            descriptor = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > MAX_RECORD_BYTES
                ):
                    raise StorageError("Daily ML credential storage has unsafe permissions or format.")
                return model.model_validate_json(source.read(MAX_RECORD_BYTES + 1))
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            raise StorageError("Could not read Daily ML credential storage.") from None

    def _replace(self, filename: str, value: BaseModel) -> None:
        """Flush a mode-0600 temporary file and atomically publish its contents."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as target:
                temporary = Path(target.name)
                os.fchmod(target.fileno(), 0o600)
                target.write(value.model_dump_json().encode())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.directory / filename)
        except OSError:
            raise StorageError("Could not save Daily ML credential storage.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def host_id(self) -> str:
        """Get or create the stable agent host ID under the credential lock.

        Returns
        -------
        str
            The installation's UUID URN.
        """
        with self.locked():
            identity = self._read("installation.json", InstallationIdentity)
            if identity is None:
                identity = InstallationIdentity(host_id=uuid4())
                self._replace("installation.json", identity)
            return identity.host_id.urn

    def read(self) -> Session:
        """Read the current atomic session snapshot.

        Returns
        -------
        Session
            Existing state or an empty initial session.
        """
        session = self._read("session.json", Session) or Session()
        if session.tokens and (
            not session.account or PLAN_SCOPE not in session.tokens.scope.split()
        ):
            raise StorageError("Could not validate Daily ML credential storage.")
        if session.account and (
            not CLIENT_ID.fullmatch(session.account.client_id)
            or session.account.client_id == "dynamic_agent_client"
        ):
            raise StorageError("Could not validate Daily ML credential storage.")
        return session

    def replace(self, session: Session) -> None:
        """Commit a new snapshot while the caller holds the credential lock.

        Parameters
        ----------
        session : Session
            New complete snapshot to commit.
        """
        self._replace("session.json", session)
