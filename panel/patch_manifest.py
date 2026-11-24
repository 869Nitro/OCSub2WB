#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replace the RT_MANIFEST resource of a PE file.

MinGW's gcc specs unconditionally inject `default-manifest.o` on every link, so
adding our own manifest through windres yields "multiple non-default manifests".
Instead we link plainly and swap resource 24/1 afterwards with UpdateResourceW.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

RT_MANIFEST = 24
LANG_EN_US = 0x0409

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
BeginUpdateResourceW = kernel32.BeginUpdateResourceW
BeginUpdateResourceW.restype = wintypes.HANDLE
BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]

UpdateResourceW = kernel32.UpdateResourceW
UpdateResourceW.restype = wintypes.BOOL
UpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.WORD, wintypes.LPVOID, wintypes.DWORD]

EndUpdateResourceW = kernel32.EndUpdateResourceW
EndUpdateResourceW.restype = wintypes.BOOL
EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

LoadLibraryExW = kernel32.LoadLibraryExW
LoadLibraryExW.restype = wintypes.HMODULE
LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]

FreeLibrary = kernel32.FreeLibrary
FreeLibrary.argtypes = [wintypes.HMODULE]

FindResourceW = kernel32.FindResourceW
FindResourceW.restype = wintypes.HANDLE
FindResourceW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR, wintypes.LPCWSTR]

LoadResource = kernel32.LoadResource
LoadResource.restype = wintypes.HANDLE
LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]

LockResource = kernel32.LockResource
LockResource.restype = wintypes.LPVOID
LockResource.argtypes = [wintypes.HANDLE]

SizeofResource = kernel32.SizeofResource
SizeofResource.restype = wintypes.DWORD
SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]


def atom(value: int):
    return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)


def read_manifest(exe: str) -> bytes:
    mod = kernel32.LoadLibraryExW(exe, None, 0x00000002 | 0x00000020)  # datafile | as_image?
    if not mod:
        mod = LoadLibraryExW(exe, None, 0x00000002)
    if not mod:
        return b""
    try:
        res = FindResourceW(mod, atom(1), atom(RT_MANIFEST))
        if not res:
            return b""
        size = SizeofResource(mod, res)
        data = LockResource(LoadResource(mod, res))
        return ctypes.string_at(data, size)
    finally:
        FreeLibrary(mod)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: patch_manifest.py <exe> <manifest.xml>", flush=True)
        return 2
    exe, manifest = argv[1], argv[2]
    payload = open(manifest, "rb").read()
    if b"Common-Controls" not in payload:
        print("[x] manifest does not include Common-Controls v6", flush=True)
        return 3

    handle = BeginUpdateResourceW(exe, False)
    if not handle:
        print(f"[x] BeginUpdateResource failed: {ctypes.get_last_error()}", flush=True)
        return 4
    ok = UpdateResourceW(handle, atom(RT_MANIFEST), atom(1), LANG_EN_US,
                         payload, len(payload))
    if not ok:
        print(f"[x] UpdateResource failed: {ctypes.get_last_error()}", flush=True)
        EndUpdateResourceW(handle, True)
        return 5
    if not EndUpdateResourceW(handle, False):
        print(f"[x] EndUpdateResource failed: {ctypes.get_last_error()}", flush=True)
        return 6

    written = read_manifest(exe)
    if b"Common-Controls" in written:
        print(f"[ok] manifest replaced ({len(payload)} bytes)")
        return 0
    print("[!] manifest replaced but read-back verification failed", flush=True)
    return 7


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
