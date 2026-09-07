"""Windows-protected local storage for reusable browser cookies.

The value is never returned by an API or written to logs.  Windows DPAPI ties
the encrypted bytes to the current Windows user, so copying this file to
another account does not reveal the cookie.
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class ProtectedCookieStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data)
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    @staticmethod
    def _bytes(blob: _DataBlob) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    @staticmethod
    def _crypt_functions():
        crypt32 = ctypes.windll.crypt32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return crypt32, kernel32

    def save(self, cookie: str) -> None:
        if os.name != "nt" or not cookie:
            return
        try:
            source, source_buffer = self._blob(cookie.encode("utf-8"))
            result = _DataBlob()
            crypt32, kernel32 = self._crypt_functions()
            if not crypt32.CryptProtectData(
                ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
            ):
                raise ctypes.WinError()
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_bytes(self._bytes(result))
            finally:
                kernel32.LocalFree(result.pbData)
            del source_buffer
        except Exception as exc:
            logger.warning("无法保存受保护的微博登录状态：%s", exc)

    def load(self) -> str:
        if os.name != "nt" or not self._path.exists():
            return ""
        try:
            source, source_buffer = self._blob(self._path.read_bytes())
            result = _DataBlob()
            crypt32, kernel32 = self._crypt_functions()
            if not crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
            ):
                raise ctypes.WinError()
            try:
                return self._bytes(result).decode("utf-8")
            finally:
                kernel32.LocalFree(result.pbData)
                del source_buffer
        except Exception as exc:
            logger.warning("无法读取受保护的微博登录状态：%s", exc)
            return ""
