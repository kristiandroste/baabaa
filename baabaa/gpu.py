"""The GPU: NVIDIA readings through NVML (part of the driver), loaded with ctypes, or an Apple silicon GPU.

NVIDIA, read-only: memory, utilization, power, temperature and the cumulative energy counter. When NVML is
missing, every reading is None and baabaa still runs; the model gateway's residency check does not
depend on NVML.

Apple silicon: the GPU shares the computer's memory, and Metal lets it hold a working set of about two
thirds of it (three quarters above 36 GB), or what `sysctl iogpu.wired_limit_mb` sets. That is the "VRAM"
here. There are no per-second readings without root, so `readings` is False. An Intel Mac has no GPU that
Ollama computes on: `available` is False and models cannot run.
"""

import ctypes
import ctypes.util
import platform
import threading

from .system import MAC, sysctl


def apple_gpu_memory(memsize: int, wired_limit_mb: int = 0) -> int:
    """Bytes of memory Metal lets the GPU keep: the wired limit when one is set, else Metal's default
    (about 2/3 of memory up to 36 GB, 3/4 above; unverified rule of thumb)."""
    if wired_limit_mb > 0:
        return wired_limit_mb * 2**20
    return memsize * 3 // 4 if memsize > 36 * 2**30 else memsize * 2 // 3


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Process(ctypes.Structure):  # nvmlProcessInfo_t (v2/v3 layout)
    _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong), ("gpu_instance", ctypes.c_uint),
                ("compute_instance", ctypes.c_uint)]


class GPU:
    def __init__(self, index: int = 0):
        self._lib = None
        self._handle = None
        self._lock = threading.Lock()
        self.name = None
        self.error = None
        self.kind = None          # "nvidia" or "apple"
        self._apple_total = None
        if MAC:
            self._apple()
            return
        try:
            path = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
            lib = ctypes.CDLL(path)
            if lib.nvmlInit_v2() != 0:
                raise OSError("nvmlInit failed")
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                raise OSError(f"no GPU at index {index}")
            buf = ctypes.create_string_buffer(96)
            lib.nvmlDeviceGetName(handle, buf, 96)
            self._lib, self._handle, self.name = lib, handle, buf.value.decode(errors="replace")
            self.kind = "nvidia"
        except OSError as exc:
            self.error = str(exc)

    def _apple(self) -> None:
        if platform.machine() != "arm64":
            self.error = "an Intel Mac: Ollama computes on the CPU here, so baabaa cannot run models"
            return
        brand = sysctl("machdep.cpu.brand_string") or "Apple silicon"
        try:
            memsize = int(sysctl("hw.memsize") or 0)
            wired = int(sysctl("iogpu.wired_limit_mb") or 0)
        except ValueError:
            memsize, wired = 0, 0
        self.name = f"{brand} GPU"
        self.kind = "apple"
        self._apple_total = apple_gpu_memory(memsize, wired) if memsize else None

    @property
    def available(self) -> bool:
        """A GPU that models can run on."""
        return self._lib is not None or self.kind == "apple"

    @property
    def readings(self) -> bool:
        """Per-second readings (utilization, power, temperature, energy, per-process memory): NVIDIA only."""
        return self._lib is not None

    def _call(self, fn: str, *args) -> bool:
        with self._lock:
            return getattr(self._lib, fn)(self._handle, *args) == 0

    def memory(self) -> dict | None:
        if self.kind == "apple":
            return {"total": self._apple_total, "used": None, "free": None} if self._apple_total else None
        if not self.readings:
            return None
        m = _Memory()
        if not self._call("nvmlDeviceGetMemoryInfo", ctypes.byref(m)):
            return None
        return {"total": m.total, "used": m.used, "free": m.free}

    def utilization(self) -> dict | None:
        if not self.readings:
            return None
        u = _Utilization()
        if not self._call("nvmlDeviceGetUtilizationRates", ctypes.byref(u)):
            return None
        return {"gpu": u.gpu, "memory": u.memory}

    def processes(self) -> list[dict] | None:
        """Programs holding GPU memory: [{'pid', 'used'}] (bytes). None when NVML cannot tell."""
        if not self.readings:
            return None
        for fn in ("nvmlDeviceGetComputeRunningProcesses_v3", "nvmlDeviceGetComputeRunningProcesses_v2"):
            if not hasattr(self._lib, fn):
                continue
            count = ctypes.c_uint(64)
            arr = (_Process * 64)()
            if self._call(fn, ctypes.byref(count), arr):
                return [{"pid": arr[i].pid, "used": arr[i].used if arr[i].used < 2**62 else None}
                        for i in range(min(count.value, 64))]
        return None

    def process_used(self, pid: int) -> int | None:
        for p in self.processes() or []:
            if p["pid"] == pid:
                return p["used"]
        return None

    def cuda_driver(self) -> int | None:
        """The newest CUDA version the driver supports, as 1000*major + 10*minor (13000 = 13.0)."""
        if not self.readings:
            return None
        v = ctypes.c_int()
        for fn in ("nvmlSystemGetCudaDriverVersion_v2", "nvmlSystemGetCudaDriverVersion"):
            if hasattr(self._lib, fn):
                with self._lock:
                    if getattr(self._lib, fn)(ctypes.byref(v)) == 0:
                        return v.value
        return None

    def power_mw(self) -> int | None:
        return self._uint("nvmlDeviceGetPowerUsage")

    def temperature_c(self) -> int | None:
        if not self.readings:
            return None
        v = ctypes.c_uint()
        return v.value if self._call("nvmlDeviceGetTemperature", 0, ctypes.byref(v)) else None

    def energy_mj(self) -> int | None:
        """Energy used since the driver loaded, in millijoules (Volta and newer)."""
        if not self.readings:
            return None
        v = ctypes.c_ulonglong()
        return v.value if self._call("nvmlDeviceGetTotalEnergyConsumption", ctypes.byref(v)) else None

    def _uint(self, fn: str) -> int | None:
        if not self.readings:
            return None
        v = ctypes.c_uint()
        return v.value if self._call(fn, ctypes.byref(v)) else None

    def sample(self) -> dict:
        mem, util = self.memory() or {}, self.utilization() or {}
        return {
            "vram_used": mem.get("used"),
            "vram_total": mem.get("total"),
            "util_gpu": util.get("gpu"),
            "util_mem": util.get("memory"),
            "power_mw": self.power_mw(),
            "temp_c": self.temperature_c(),
            "energy_mj": self.energy_mj(),
        }
