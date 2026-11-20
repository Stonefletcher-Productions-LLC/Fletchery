"""One secret in Windows Credential Manager (a generic credential for the signed-in user), so an API key never sits in
a file, a profile or the repository. Standard library only (ctypes).

    python -m fletchery.credstore set Fletchery:anthropic-api-key      (asks for the value without echoing it)

or from a console: `cmdkey /generic:Fletchery:anthropic-api-key /user:anthropic /pass:<key>`. Off Windows nothing is
stored and `read` returns "" (tests use the environment fallback in mergehelp.py).
"""
from __future__ import annotations

import os
import sys

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def _api():
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, adv


def read(target: str) -> str:
    if os.name != "nt":
        return ""
    ctypes, CREDENTIAL, adv = _api()
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return ""
    try:
        c = p.contents
        raw = bytes(c.CredentialBlob[i] for i in range(c.CredentialBlobSize))
    finally:
        adv.CredFree(p)
    # cmdkey stores UTF-16; CredWriteW below stores UTF-8. Either way the key is ASCII.
    if len(raw) >= 2 and raw[1:2] == b"\0":
        return raw.decode("utf-16-le", "replace").strip("\0").strip()
    return raw.decode("utf-8", "replace").strip()


def write(target: str, secret: str, user: str = "fletchery") -> None:
    if os.name != "nt":
        raise OSError("Windows Credential Manager is only on Windows.")
    ctypes, CREDENTIAL, adv = _api()
    blob = secret.encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL()
    c.Type = CRED_TYPE_GENERIC
    c.TargetName = target
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = CRED_PERSIST_LOCAL_MACHINE
    c.UserName = user
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(f"CredWriteW failed ({ctypes.get_last_error()})")


def delete(target: str) -> bool:
    if os.name != "nt":
        return False
    _, _, adv = _api()
    return bool(adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "set":
        import getpass
        write(sys.argv[2], getpass.getpass("Value (not shown): ").strip())
        print("Saved.")
    elif len(sys.argv) == 3 and sys.argv[1] == "delete":
        print("Deleted." if delete(sys.argv[2]) else "Nothing to delete.")
    else:
        print(__doc__)
