"""
core/gpu_stats.py — Lightweight DXGI video-memory usage query.

Uses public DXGI adapter memory budgets/usages without injecting into games.
If DXGI is unavailable or unsupported, returns None gracefully.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from core.models import GpuMemorySnapshot


DXGI_ADAPTER_FLAG_SOFTWARE = 2
DXGI_MEMORY_SEGMENT_GROUP_LOCAL = 0
DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL = 1
DXGI_ERROR_NOT_FOUND = 0x887A0002
S_OK = 0
HRESULT = getattr(wintypes, "HRESULT", ctypes.c_long)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", ctypes.c_longlong),
        ("Flags", wintypes.UINT),
    ]


class DXGI_QUERY_VIDEO_MEMORY_INFO(ctypes.Structure):
    _fields_ = [
        ("Budget", ctypes.c_ulonglong),
        ("CurrentUsage", ctypes.c_ulonglong),
        ("AvailableForReservation", ctypes.c_ulonglong),
        ("CurrentReservation", ctypes.c_ulonglong),
    ]


class IUnknown(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(ctypes.c_void_p))]


def _guid_from_str(value: str) -> GUID:
    import uuid

    u = uuid.UUID(value)
    data4 = (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:])
    return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)


_IID_IDXGIFactory1 = _guid_from_str("770aae78-f26f-4dba-a829-253c83d1b387")


dxgi = ctypes.WinDLL("dxgi")
CreateDXGIFactory1 = dxgi.CreateDXGIFactory1
CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
CreateDXGIFactory1.restype = HRESULT


def _call_com_method(obj_ptr: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_addr = vtbl[index]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(fn_addr)


def _release(obj_ptr: ctypes.c_void_p):
    if not obj_ptr:
        return
    release = _call_com_method(obj_ptr, 2, wintypes.ULONG)
    release(obj_ptr)


def query_gpu_memory() -> GpuMemorySnapshot | None:
    factory_ptr = ctypes.c_void_p()
    hr = CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory_ptr))
    if hr != S_OK or not factory_ptr:
        return None

    total_local_usage = 0
    total_local_budget = 0
    total_shared_usage = 0
    total_shared_budget = 0

    enum_adapters1 = _call_com_method(factory_ptr, 12, HRESULT, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
    get_desc1 = None
    query_video_memory_info = None
    try:
        index = 0
        while True:
            adapter_ptr = ctypes.c_void_p()
            hr = enum_adapters1(factory_ptr, index, ctypes.byref(adapter_ptr))
            if hr == DXGI_ERROR_NOT_FOUND:
                break
            if hr != S_OK or not adapter_ptr:
                break
            try:
                if get_desc1 is None:
                    get_desc1 = _call_com_method(adapter_ptr, 11, HRESULT, ctypes.POINTER(DXGI_ADAPTER_DESC1))
                desc = DXGI_ADAPTER_DESC1()
                if get_desc1(adapter_ptr, ctypes.byref(desc)) != S_OK:
                    index += 1
                    continue
                if desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE:
                    index += 1
                    continue
                if query_video_memory_info is None:
                    query_video_memory_info = _call_com_method(
                        adapter_ptr,
                        15,
                        HRESULT,
                        wintypes.UINT,
                        ctypes.c_int,
                        ctypes.POINTER(DXGI_QUERY_VIDEO_MEMORY_INFO),
                    )
                local = DXGI_QUERY_VIDEO_MEMORY_INFO()
                shared = DXGI_QUERY_VIDEO_MEMORY_INFO()
                if query_video_memory_info(adapter_ptr, 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, ctypes.byref(local)) == S_OK:
                    total_local_usage += local.CurrentUsage
                    total_local_budget += local.Budget
                if query_video_memory_info(adapter_ptr, 0, DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL, ctypes.byref(shared)) == S_OK:
                    total_shared_usage += shared.CurrentUsage
                    total_shared_budget += shared.Budget
            finally:
                _release(adapter_ptr)
            index += 1
    finally:
        _release(factory_ptr)

    if total_local_budget <= 0 and total_shared_budget <= 0:
        return None

    local_usage_mb = total_local_usage / (1024 * 1024)
    local_budget_mb = total_local_budget / (1024 * 1024)
    shared_usage_mb = total_shared_usage / (1024 * 1024)
    shared_budget_mb = total_shared_budget / (1024 * 1024)
    local_ratio = (local_usage_mb / local_budget_mb) if local_budget_mb > 0 else 0.0
    shared_ratio = (shared_usage_mb / shared_budget_mb) if shared_budget_mb > 0 else 0.0
    return GpuMemorySnapshot(
        local_usage_mb=local_usage_mb,
        local_budget_mb=local_budget_mb,
        shared_usage_mb=shared_usage_mb,
        shared_budget_mb=shared_budget_mb,
        local_usage_ratio=local_ratio,
        shared_usage_ratio=shared_ratio,
    )
