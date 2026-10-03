"""Vector similarity search on the GPU, through the NVIDIA driver API with ctypes (no packages).

A hand-written PTX kernel computes one dot product per stored vector; the driver compiles it for the
installed GPU. The search runs in a short-lived worker process (`python3 cuda.py`, fed on stdin), so the
server never keeps CUDA state: the context (about 165 MiB of VRAM) and the driver's host memory are
released when the worker exits. The gateway runs the worker inside the GPU queue. There is no CPU
fallback: without a usable GPU, retrieval falls back to keyword search and says so.

Worker protocol. stdin: one JSON line {"n", "d", "k"}, then n*d float32 (the rows), then d float32 (the
query). stdout: one JSON line {"top": [[row, score], ...]} or {"error": "..."}.
"""

import array
import ctypes
import ctypes.util
import heapq
import json
import math
import sys
import threading
import time

PTX = r"""
.version 7.0
.target sm_52
.address_size 64

.visible .entry dot_rows(
    .param .u64 p_mat, .param .u64 p_q, .param .u64 p_out, .param .u32 p_n, .param .u32 p_d)
{
    .reg .pred %p<3>;
    .reg .b32 %r<9>;
    .reg .f32 %f<4>;
    .reg .b64 %rd<12>;

    ld.param.u64 %rd1, [p_mat];
    ld.param.u64 %rd2, [p_q];
    ld.param.u64 %rd3, [p_out];
    ld.param.u32 %r1, [p_n];
    ld.param.u32 %r2, [p_d];
    mov.u32 %r3, %ctaid.x;
    mov.u32 %r4, %ntid.x;
    mov.u32 %r5, %tid.x;
    mad.lo.s32 %r6, %r3, %r4, %r5;
    setp.ge.u32 %p1, %r6, %r1;
    @%p1 bra DONE;
    cvta.to.global.u64 %rd4, %rd1;
    cvta.to.global.u64 %rd5, %rd2;
    cvta.to.global.u64 %rd6, %rd3;
    mul.wide.u32 %rd7, %r6, %r2;
    shl.b64 %rd7, %rd7, 2;
    add.s64 %rd8, %rd4, %rd7;
    mov.u64 %rd9, %rd5;
    mov.f32 %f1, 0f00000000;
    mov.u32 %r7, 0;
LOOP:
    setp.ge.u32 %p2, %r7, %r2;
    @%p2 bra END;
    ld.global.f32 %f2, [%rd8];
    ld.global.f32 %f3, [%rd9];
    fma.rn.f32 %f1, %f2, %f3, %f1;
    add.s64 %rd8, %rd8, 4;
    add.s64 %rd9, %rd9, 4;
    add.u32 %r7, %r7, 1;
    bra LOOP;
END:
    mul.wide.u32 %rd10, %r6, 4;
    add.s64 %rd11, %rd6, %rd10;
    st.global.f32 [%rd11], %f1;
DONE:
    ret;
}
"""


class CudaError(Exception):
    pass


class CudaSearch:
    def __init__(self):
        self._lib = None
        self._lock = threading.Lock()
        self.error = None
        try:
            self._lib = ctypes.CDLL(ctypes.util.find_library("cuda") or "libcuda.so.1")
            self._check(self._lib.cuInit(0), "cuInit")
        except (OSError, CudaError) as exc:
            self._lib, self.error = None, str(exc)

    @property
    def available(self) -> bool:
        return self._lib is not None

    def _check(self, res: int, what: str) -> None:
        if res != 0:
            msg = ctypes.c_char_p()
            if self._lib is not None:
                self._lib.cuGetErrorString(res, ctypes.byref(msg))
            raise CudaError(f"{what} failed: {(msg.value or b'').decode() or res}")

    def scores(self, matrix: bytes, n: int, d: int, query: list[float]) -> list[float]:
        """Dot products of `query` with each of the n rows of a float32 row-major matrix, on the GPU."""
        if not self.available:
            raise CudaError(self.error or "no CUDA driver")
        if n == 0:
            return []
        if len(matrix) != n * d * 4 or len(query) != d:
            raise CudaError("matrix and query sizes do not match")
        cu = self._lib
        with self._lock:
            dev = ctypes.c_int()
            ctx = ctypes.c_void_p()
            self._check(cu.cuDeviceGet(ctypes.byref(dev), 0), "cuDeviceGet")
            self._check(cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate")
            try:
                mod, fn = ctypes.c_void_p(), ctypes.c_void_p()
                self._check(cu.cuModuleLoadData(ctypes.byref(mod), ctypes.c_char_p(PTX.encode())), "cuModuleLoadData")
                self._check(cu.cuModuleGetFunction(ctypes.byref(fn), mod, b"dot_rows"), "cuModuleGetFunction")
                d_mat, d_q, d_out = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
                self._check(cu.cuMemAlloc_v2(ctypes.byref(d_mat), ctypes.c_size_t(n * d * 4)), "cuMemAlloc")
                self._check(cu.cuMemAlloc_v2(ctypes.byref(d_q), ctypes.c_size_t(d * 4)), "cuMemAlloc")
                self._check(cu.cuMemAlloc_v2(ctypes.byref(d_out), ctypes.c_size_t(n * 4)), "cuMemAlloc")
                q = array.array("f", query).tobytes()
                self._check(cu.cuMemcpyHtoD_v2(d_mat, ctypes.c_char_p(matrix), ctypes.c_size_t(n * d * 4)), "copy")
                self._check(cu.cuMemcpyHtoD_v2(d_q, ctypes.c_char_p(q), ctypes.c_size_t(d * 4)), "copy")
                c_n, c_d = ctypes.c_uint32(n), ctypes.c_uint32(d)
                params = (ctypes.c_void_p * 5)(*[ctypes.cast(ctypes.byref(x), ctypes.c_void_p)
                                                  for x in (d_mat, d_q, d_out, c_n, c_d)])
                block = 128
                grid = (n + block - 1) // block
                self._check(cu.cuLaunchKernel(fn, grid, 1, 1, block, 1, 1, 0, None, params, None), "cuLaunchKernel")
                self._check(cu.cuCtxSynchronize(), "cuCtxSynchronize")
                out = (ctypes.c_float * n)()
                self._check(cu.cuMemcpyDtoH_v2(out, d_out, ctypes.c_size_t(n * 4)), "copy back")
                for ptr in (d_mat, d_q, d_out):
                    cu.cuMemFree_v2(ptr)
                cu.cuModuleUnload(mod)
                return list(out)
            finally:
                cu.cuCtxDestroy_v2(ctx)


def normalize(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def pack(vectors: list[list[float]]) -> bytes:
    out = array.array("f")
    for v in vectors:
        out.extend(normalize(v))
    return out.tobytes()


def main() -> int:
    t0 = time.monotonic()
    stdin = sys.stdin.buffer
    try:
        head = json.loads(stdin.readline())
        n, d, k = int(head["n"]), int(head["d"]), int(head.get("k") or 10)
        matrix = stdin.read(n * d * 4)
        query = array.array("f")
        query.frombytes(stdin.read(d * 4))
        search = CudaSearch()
        scores = search.scores(matrix, n, d, list(query))
        top = heapq.nlargest(min(k, n), range(n), key=scores.__getitem__)
        out = {"top": [[i, round(scores[i], 6)] for i in top], "ms": round((time.monotonic() - t0) * 1000)}
        code = 0
    except (CudaError, ValueError, KeyError, OSError) as exc:
        out, code = {"error": str(exc)}, 1
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
