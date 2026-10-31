"""OS credential stores for the Bridge password and the MCP bearer token.

Secrets reach a store through stdin or a native API, never through a command
line, and no error message carries store output.
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping

STORES = ("keychain", "libsecret", "pass", "wincred")
# The Bridge password keeps the service name the macOS Keychain path always used.
BRIDGE_SERVICE = "proton-mail-mcp"
TOKEN_SERVICE = "proton-mail-mcp-token"
SECURITY = "/usr/bin/security"
_LABELS = {
    BRIDGE_SERVICE: "proton-mail-mcp Bridge password",
    TOKEN_SERVICE: "proton-mail-mcp MCP bearer token",
}
# Keychain items written through `security -i` are quoted command lines, so
# both the account and a non-interactive secret are limited to quote-free text.
_KEYCHAIN_SAFE = re.compile(r"[!#-&(-\[\]-~]+")
_TIMEOUT = 10
# Non-secret settings that locate a pass store and its GnuPG home. Services do not
# inherit the shell that ran the installer, so every pass call receives them.
LOCATION_SETTINGS = (
    ("PROTON_MCP_PASSWORD_STORE_DIR", "PASSWORD_STORE_DIR"),
    ("PROTON_MCP_GNUPGHOME", "GNUPGHOME"),
)
# Windows: CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE and ERROR_NOT_FOUND.
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class CredentialStoreError(RuntimeError):
    """A credential store operation failed; store output is intentionally omitted."""


def validate_store(store: str) -> str:
    if store not in STORES:
        raise CredentialStoreError("Credential store must be keychain, libsecret, pass or wincred")
    return store


def validate_account(account: str) -> str:
    """Accounts become pass paths and quoted Keychain arguments, so keep them plain."""
    if (
        not isinstance(account, str)
        or not 1 <= len(account) <= 254
        or account.startswith(".")
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in account)
        or any(char in account for char in "/\\\"'")
    ):
        raise CredentialStoreError(
            "Bridge account must be 1 to 254 characters without spaces, quotes or slashes"
        )
    return account


def _plain_account(account: str) -> str:
    """Every store needs nonempty account text; argv and API stores need nothing more."""
    if (
        not isinstance(account, str)
        or not account
        or any(ord(char) < 32 or ord(char) == 127 for char in account)
    ):
        raise CredentialStoreError("Bridge account must be nonempty text")
    return account


def pass_entry(service: str, account: str) -> str:
    """The account becomes a password-store path component."""
    return f"{service}/{validate_account(account)}"


def wincred_target(service: str, account: str) -> str:
    return f"{service}/{_plain_account(account)}"


def pass_locations(settings: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """PASSWORD_STORE_DIR and GNUPGHOME as named by settings; unset ones are omitted."""
    values = dict(settings)
    return {name: values[key] for key, name in LOCATION_SETTINGS if values.get(key)}


def _run(
    argv: list[str],
    *,
    input_text: str | None = None,
    locations: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    kwargs = {"capture_output": True, "text": True, "check": False, "timeout": _TIMEOUT}
    if input_text is not None:
        kwargs["input"] = input_text
    if locations:
        # pass and the gpg it runs read these; the rest of the environment is kept.
        kwargs["env"] = {**os.environ, **locations}
    return subprocess.run(argv, **kwargs)


def lookup(
    store: str, service: str, account: str, locations: Mapping[str, str] | None = None
) -> str:
    """Return the stored secret, or an empty string for an empty entry.

    `locations` (from pass_locations) applies to pass, overriding the environment.
    """
    validate_store(store)
    _plain_account(account)
    if store == "wincred":
        return _wincred_read(wincred_target(service, account))
    if store == "keychain":
        argv = [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"]
    elif store == "libsecret":
        argv = ["secret-tool", "lookup", "service", service, "account", account]
    else:
        argv = ["pass", "show", pass_entry(service, account)]
    try:
        result = _run(argv, locations=locations if store == "pass" else None)
    except (OSError, subprocess.SubprocessError):
        raise CredentialStoreError("Credential store is unavailable") from None
    if result.returncode:
        raise CredentialStoreError("Credential is unavailable from the credential store")
    if store == "pass":
        # pass keeps the secret on the first line; later lines are metadata.
        return result.stdout.split("\n", 1)[0].rstrip("\r")
    return result.stdout.rstrip("\r\n")


def pass_file(service: str, account: str, locations: Mapping[str, str] | None = None) -> str:
    root = (
        (locations or {}).get("PASSWORD_STORE_DIR")
        or os.environ.get("PASSWORD_STORE_DIR")
        or os.path.join(os.path.expanduser("~"), ".password-store")
    )
    return os.path.join(root, *pass_entry(service, account).split("/")) + ".gpg"


def exists(
    store: str, service: str, account: str, locations: Mapping[str, str] | None = None
) -> bool:
    """Report whether a nonempty entry exists; the secret itself is discarded.

    A Keychain or libsecret check that fails or times out raises CredentialStoreError,
    and so does secret-tool's "nothing found", which a locked entry gives too: of the
    two, only Keychain's own "not found" answer means absent.
    """
    validate_store(store)
    _plain_account(account)
    if store == "pass":
        # Decrypting could ask for a GPG passphrase; the entry file is enough.
        try:
            return os.path.isfile(pass_file(service, account, locations))
        except CredentialStoreError:
            return False
    if store == "keychain":
        # Without -w, security reports the item without reading its secret.
        argv = [SECURITY, "find-generic-password", "-s", service, "-a", account]
    elif store == "libsecret":
        argv = ["secret-tool", "lookup", "service", service, "account", account]
    else:
        try:
            return bool(lookup(store, service, account, locations))
        except CredentialStoreError:
            return False
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError):
        raise CredentialStoreError("Credential store is unavailable") from None
    if result.returncode == 0:
        return store == "keychain" or bool(result.stdout.rstrip("\r\n"))
    # security exits 44 (errSecItemNotFound) for no such item. secret-tool exits 1 and
    # prints nothing for no entry, but also for a locked one whose unlock prompt was
    # dismissed, so that leaves the entry unknown, like any other failure.
    if store == "keychain" and result.returncode == 44:
        return False
    if store == "libsecret" and result.returncode == 1 and not (result.stderr or "").strip():
        raise CredentialStoreError(
            "secret-tool found no readable entry; a locked one looks the same"
        )
    raise CredentialStoreError("Credential store did not answer")


def store_secret(
    store: str,
    service: str,
    account: str,
    secret: str,
    locations: Mapping[str, str] | None = None,
) -> None:
    """Create or replace an entry, passing the secret on stdin or through the API."""
    validate_store(store)
    _plain_account(account)
    if not secret or "\n" in secret or "\r" in secret or "\0" in secret:
        raise CredentialStoreError("Secret must be a single nonempty line")
    if store == "wincred":
        _wincred_write(wincred_target(service, account), account, secret)
        return
    if store == "keychain":
        if not keychain_accepts(account, secret):
            raise CredentialStoreError(
                "This secret needs the interactive Keychain prompt; use `security "
                "add-generic-password -U -s SERVICE -a ACCOUNT -w`"
            )
        argv = [SECURITY, "-i"]
        payload = f'add-generic-password -U -s "{service}" -a "{account}" -w "{secret}"\n'
    elif store == "libsecret":
        argv = [
            "secret-tool",
            "store",
            f"--label={_LABELS.get(service, service)}",
            "service",
            service,
            "account",
            account,
        ]
        payload = secret
    else:
        argv = ["pass", "insert", "--multiline", "--force", pass_entry(service, account)]
        payload = secret + "\n"
    try:
        result = _run(argv, input_text=payload, locations=locations if store == "pass" else None)
    except (OSError, subprocess.SubprocessError):
        raise CredentialStoreError("Credential store is unavailable") from None
    if result.returncode:
        raise CredentialStoreError("Credential store refused the entry")


def remove_secret(
    store: str, service: str, account: str, locations: Mapping[str, str] | None = None
) -> bool:
    """Remove an entry; returns False when there was nothing to remove."""
    validate_store(store)
    _plain_account(account)
    if store == "wincred":
        return _wincred_delete(wincred_target(service, account))
    if not exists(store, service, account, locations):
        return False
    if store == "keychain":
        argv = [SECURITY, "delete-generic-password", "-s", service, "-a", account]
    elif store == "libsecret":
        argv = ["secret-tool", "clear", "service", service, "account", account]
    else:
        argv = ["pass", "rm", "--force", pass_entry(service, account)]
    try:
        result = _run(argv, locations=locations if store == "pass" else None)
    except (OSError, subprocess.SubprocessError):
        raise CredentialStoreError("Credential store is unavailable") from None
    if result.returncode:
        raise CredentialStoreError("Credential store did not remove the entry")
    return True


def keychain_accepts(account: str, secret: str) -> bool:
    """Whether `security -i` can take this entry on stdin without quoting hazards."""
    return bool(_KEYCHAIN_SAFE.fullmatch(account) and _KEYCHAIN_SAFE.fullmatch(secret))


def keychain_prompt_argv(service: str, account: str) -> list[str]:
    """`security` prompts for the secret itself when -w is the final argument."""
    return [SECURITY, "add-generic-password", "-U", "-s", service, "-a", account, "-w"]


def _wincred_api():
    if sys.platform != "win32":
        raise CredentialStoreError("Windows Credential Manager is only available on Windows")
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(CREDENTIAL)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return ctypes, advapi32, CREDENTIAL, pointer


def _wincred_read(target: str) -> str:
    ctypes, advapi32, _credential, pointer = _wincred_api()
    found = pointer()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        raise CredentialStoreError("Credential is unavailable from Windows Credential Manager")
    try:
        size = found.contents.CredentialBlobSize
        blob = ctypes.string_at(found.contents.CredentialBlob, size) if size else b""
    finally:
        advapi32.CredFree(found)
    try:
        return blob.decode("utf-16-le")
    except UnicodeDecodeError:
        raise CredentialStoreError("Windows credential is not UTF-16 text") from None


def _wincred_write(target: str, account: str, secret: str) -> None:
    ctypes, advapi32, credential_type, _pointer = _wincred_api()
    blob = secret.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = credential_type()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = account
    try:
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError("Windows Credential Manager refused the entry")
    finally:
        ctypes.memset(buffer, 0, len(blob))


def _wincred_delete(target: str) -> bool:
    ctypes, advapi32, _credential, _pointer = _wincred_api()
    if advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == _ERROR_NOT_FOUND:
        return False
    raise CredentialStoreError("Windows Credential Manager did not remove the entry")
