"""Read the metadata of a GGUF model file (llama.cpp's format) without loading the model.

Only the header is read: key/value metadata and the tensor directory. Long arrays (the tokenizer's
vocabulary) are skipped over, not kept. Used to describe models that run outside Ollama and to
estimate their memory needs before the GPU fit test confirms them.
"""

import struct

MAGIC = b"GGUF"
_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9


class GGUFError(Exception):
    pass


class _Reader:
    def __init__(self, f):
        self.f = f

    def read(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise GGUFError("unexpected end of file")
        return b

    def scalar(self, t: int):
        fmt = _SCALAR[t]
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        (n,) = struct.unpack("<Q", self.read(8))
        if n > 1 << 24:
            raise GGUFError("string too long")
        return self.read(n).decode("utf-8", "replace")

    def skip_string(self) -> None:
        (n,) = struct.unpack("<Q", self.read(8))
        self.f.seek(n, 1)

    def value(self, t: int, max_array: int):
        if t in _SCALAR:
            return self.scalar(t)
        if t == _STRING:
            return self.string()
        if t == _ARRAY:
            (et,) = struct.unpack("<I", self.read(4))
            (count,) = struct.unpack("<Q", self.read(8))
            if count > max_array:
                if et in _SCALAR:
                    self.f.seek(count * struct.calcsize(_SCALAR[et]), 1)
                elif et == _STRING:
                    for _ in range(count):
                        self.skip_string()
                else:
                    for _ in range(count):
                        self.value(et, 0)
                return {"array_of": et, "count": count}
            return [self.value(et, max_array) for _ in range(count)]
        raise GGUFError(f"unknown value type {t}")


# Input lookup tables: llama.cpp (and Ollama) keep these in system memory and look the input tokens up
# there, one row per token; the rest of the model is on the GPU.
LOOKUP_TABLES = ("token_embd.weight", "per_layer_token_embd.weight", "token_types.weight", "position_embd.weight")
# Image/audio encoder tensors, when a model file carries its own encoder (Ollama's newer model files do).
ENCODER_PREFIXES = ("v.", "mm.", "a.")


def read(path: str, max_array: int = 64) -> dict:
    """{'version', 'metadata': {key: value}, 'tensors': {'count', 'layers', 'names_sample', 'bytes', ...}}.

    Tensor sizes come from the gaps between data offsets, so they are exact whatever the (possibly
    custom) quantization types are."""
    import os
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        r = _Reader(f)
        if r.read(4) != MAGIC:
            raise GGUFError("not a GGUF file")
        version, n_tensors, n_kv = struct.unpack("<IQQ", r.read(20))
        if version < 2 or n_kv > 100_000 or n_tensors > 1_000_000:
            raise GGUFError(f"unsupported GGUF header (version {version})")
        meta = {}
        for _ in range(n_kv):
            key = r.string()
            (t,) = struct.unpack("<I", r.read(4))
            meta[key] = r.value(t, max_array)
        layers, sample, offsets = set(), [], []
        for _ in range(n_tensors):
            name = r.string()
            (nd,) = struct.unpack("<I", r.read(4))
            r.read(8 * nd + 4)  # dims, type
            (offset,) = struct.unpack("<Q", r.read(8))
            offsets.append((offset, name))
            if name.startswith("blk."):
                layers.add(name.split(".")[1])
            if len(sample) < 8:
                sample.append(name)
        align = int(meta.get("general.alignment") or 32)
        data_start = -(-f.tell() // align) * align
    offsets.sort()
    sizes = {}
    for i, (off, name) in enumerate(offsets):
        end = offsets[i + 1][0] if i + 1 < len(offsets) else size - data_start
        sizes[name] = max(0, end - off)
    return {"version": version, "metadata": meta,
            "tensors": {"count": n_tensors, "layers": len(layers), "names_sample": sample,
                        "bytes": sum(sizes.values()), "token_embd_bytes": sum(sizes.get(n, 0) for n in LOOKUP_TABLES),
                        "output_bytes": sizes.get("output.weight", 0),
                        "encoder_bytes": sum(v for k, v in sizes.items() if k.startswith(ENCODER_PREFIXES)),
                        "largest": sorted(
                            sizes.items(), key=lambda kv: -kv[1])[:5]}}


def describe(path: str) -> dict:
    """The facts baabaa needs about a model file, in the same shape as Ollama-derived model info."""
    g = read(path)
    m = g["metadata"]
    arch = m.get("general.architecture") or ""
    get = lambda key, default=None: m.get(f"{arch}.{key}", default)  # noqa: E731
    template = m.get("tokenizer.chat_template") or ""
    caps = ["completion"]
    if isinstance(template, str) and "tools" in template:
        caps.append("tools")
    if isinstance(template, str) and ("<think>" in template or "enable_thinking" in template or "reasoning" in template):
        caps.append("thinking")
    heads_kv = get("attention.head_count_kv")
    if isinstance(heads_kv, list):
        heads_kv = max(heads_kv) if heads_kv else None
    head_dim = get("attention.key_length") or (
        (get("embedding_length") or 0) // (get("attention.head_count") or 1) if get("attention.head_count") else None)
    return {
        "arch": arch,
        "name": m.get("general.name"),
        "basename": m.get("general.basename"),
        "size_label": m.get("general.size_label"),
        "license": m.get("general.license"),
        "file_type": m.get("general.file_type"),
        "context_length": get("context_length"),
        "block_count": get("block_count"),
        "embedding_length": get("embedding_length"),
        "head_count": get("attention.head_count"),
        "head_count_kv": heads_kv,
        "head_dim": head_dim,
        "full_attention_interval": get("full_attention_interval"),
        "capabilities": caps,
        "has_chat_template": bool(template),
        "tensor_count": g["tensors"]["count"],
        "layers": g["tensors"]["layers"],
        "weight_bytes": g["tensors"]["bytes"],
        "token_embd_bytes": g["tensors"]["token_embd_bytes"],
        "encoder_bytes": g["tensors"]["encoder_bytes"],
    }
