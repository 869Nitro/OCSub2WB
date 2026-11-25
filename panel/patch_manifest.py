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
from pathlib import Path

RT_MANIFEST = 24
LANG_EN_US = 0x0409
LOAD_AS_RESOURCE = 0x00000002 | 0x00000020
RESOURCE_NOT_FOUND = {1812, 1813, 1814, 1815}

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

EnumLangProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, wintypes.LPVOID, wintypes.LPVOID,
    wintypes.WORD, wintypes.LPARAM,
)
EnumResourceLanguagesW = kernel32.EnumResourceLanguagesW
EnumResourceLanguagesW.restype = wintypes.BOOL
EnumResourceLanguagesW.argtypes = [
    wintypes.HMODULE, wintypes.LPCWSTR, wintypes.LPCWSTR, EnumLangProc, wintypes.LPARAM,
]

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


def manifest_languages(exe: str) -> list[int]:
    mod = LoadLibraryExW(exe, None, LOAD_AS_RESOURCE)
    if not mod:
        raise ctypes.WinError(ctypes.get_last_error())
    languages: list[int] = []

    @EnumLangProc
    def collect(_mod, _kind, _name, language, _arg):
        languages.append(language)
        return True

    try:
        if not EnumResourceLanguagesW(mod, atom(RT_MANIFEST), atom(1), collect, 0):
            error = ctypes.get_last_error()
            if error not in RESOURCE_NOT_FOUND:
                raise ctypes.WinError(error)
        return languages
    finally:
        FreeLibrary(mod)


def read_manifest(exe: str) -> bytes:
    mod = LoadLibraryExW(exe, None, LOAD_AS_RESOURCE)
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
    exe = str(Path(argv[1]).resolve())
    payload = Path(argv[2]).read_bytes()
    if b"Common-Controls" not in payload:
        print("[x] manifest does not include Common-Controls v6", flush=True)
        return 3

    languages = manifest_languages(exe) or [LANG_EN_US]
    handle = BeginUpdateResourceW(exe, False)
    if not handle:
        print(f"[x] BeginUpdateResource failed: {ctypes.get_last_error()}", flush=True)
        return 4
    # The Windows loader can prefer the linker's manifest language over en-US.
    for language in languages:
        if not UpdateResourceW(handle, atom(RT_MANIFEST), atom(1), language,
                               payload, len(payload)):
            print(f"[x] UpdateResource failed: {ctypes.get_last_error()}", flush=True)
            EndUpdateResourceW(handle, True)
            return 5
    if not EndUpdateResourceW(handle, False):
        print(f"[x] EndUpdateResource failed: {ctypes.get_last_error()}", flush=True)
        return 6

    written = read_manifest(exe)
    if written == payload:
        print(f"[ok] manifest replaced ({len(payload)} bytes, {len(languages)} language entries)")
        return 0
    print("[!] manifest replaced but read-back verification failed", flush=True)
    return 7


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
