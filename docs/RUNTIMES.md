# Models outside Ollama, and how baabaa protects the machine

Most models run through Ollama. Some need a program of their own:
- chat models that need a special build of llama.cpp (1-bit and ternary models, for example, use their
  own quantization formats);
- image models, served by stable-diffusion.cpp's `sd-server`;
- turns with an image or audio for an Ollama model (see "Ollama models with images or audio").

baabaa runs them through the same GPU queue and the same rules as Ollama models: it starts the model's
server itself, checks where every part of the model sits and how hard the program works the CPU, and
refuses it unless the checks pass. Only one program holds the GPU at a time: loading for one unloads or
stops the others.

## What the rules protect

Model work on the CPU makes a small computer strain: fans at full speed, heat, and answers that take far
longer. So:
- all model computation (inference, image/audio encoders, embeddings, image generation) runs on the GPU,
  with never a fallback to the CPU or a model split between GPU and CPU;
- system memory stays at normal-program scale, like a browser's: caches are bounded, never open-ended;
- disk holds transcripts, logs and long-term data (projects, memory).

A model server always holds some system memory: its runtime (CUDA libraries and buffers, about
0.4–0.6 GiB measured) and the input lookup tables, which llama.cpp, and Ollama with it, keeps in system
memory and reads one row per input token (not computation). Its caches come on top, within their caps.

## The CPU guard

While a request runs, baabaa samples the CPU time of the program serving it: its own llama.cpp or
stable-diffusion.cpp server, or the Ollama process serving the model. A model on the GPU keeps about one
core busy (the thread that feeds the GPU and waits for it). More than 2.5 cores for 10 seconds means model
work is running on the CPU: baabaa stops the program (for Ollama, unloads the model and ends the request)
and the request fails with the reason. Every request's CPU seconds and busiest moment go into the
statistics (`cpu_s`, `cpu_peak`).

Measured on an 8 GB card with a 6-core laptop CPU (2026-09-30), busiest moment per request: every chat
model, Ollama's and baabaa's own servers alike, 1.0 core; image generation 1.0–1.6 cores; loading a
staged image model from disk 2.0 cores; the whole machine at most 2.2 cores busy while generating images.

Ollama 0.35.0 starts its model server with one CPU thread per core (`-t` left at its default). With every
layer on the GPU the extra threads have nothing to do and spin waiting for work (llama.cpp's `--poll 50`):
3.5 cores busy for the whole reply, one thread at 1.00 and five at 0.49 each (2026-10-01). The guard then
stopped any reply longer than 10 seconds. With one thread the same work keeps 1.0 core busy at the same
speed: nemotron-3-nano:4b wrote 149.6 tokens/s with 6 threads and 149.9 with 1; qwen3.5:9b read an
18,815-token prompt at 2,635 and 2,623 tokens/s and wrote at 64.5 and 66.5 tokens/s. So every Ollama
request baabaa sends carries `num_thread: 1` (a load-time setting, the same in every request, so it never
forces a reload), and baabaa starts its own llama.cpp servers with `-t 1`. Measured afterwards, replaying
a real conversation: busiest moment 1.03 cores (nemotron-3-nano:4b) and 1.04 (qwen3.5:9b).

## Registering a model

On the host:

```sh
baabaa models add-llamacpp NAME --server /path/to/llama-server --file /path/to/model.gguf \
    [--lib /path/to/extra/libs] [--mmproj /path/to/vision-projector.gguf] [--arg=-fa --arg=on]
baabaa models test NAME       # the GPU fit test (loads the model at growing context sizes)
```

Or in **Settings → Models → Models from other programs**, which asks for the owner's password because it
starts a program on this computer. The name uses lower-case letters, digits, `.`, `_` and `-`; the
files stay where they are and are only read. After a passing fit test the model is approved like any
other and appears in the model picker. Removing it only unregisters it.

baabaa reads the model file's header (GGUF) for its architecture, context length, layer layout, chat
template (tool calls, thinking) and exact tensor sizes, without loading it.

## How baabaa runs a llama.cpp server

One llama.cpp server at a time, started for a model at the fit test's context size and stopped after the
same idle time as Ollama models (`keep_alive`), or when another program needs the GPU.

- Command: `llama-server -m MODEL --host 127.0.0.1 --port <free port> -c <context> -np 2 --kv-unified
  --no-cache-idle-slots -ngl 999 --no-mmap --jinja -dev CUDA0 --cache-ram 1024 --ctx-checkpoints 4
  --checkpoint-min-step 256 -t 1 -lv 4 --no-webui` plus the registered extra arguments (`-t 1`: see The CPU guard). Flags a build does not
  list in its `--help` are left out (and it gets one slot without `--kv-unified`).
- `-dev CUDA0` makes a server without a working GPU fail instead of loading on the CPU.
- Two slots over one shared context: a short side request (a conversation's title, the auto-mode judge)
  runs in the second slot, and the conversation's context stays in VRAM. With one slot it was swapped
  out, and the next turn reprocessed the whole conversation (measured: 4–5 s instead of 0.6 s on a 27B
  model; the second slot's recurrent state costs 0.14 GiB of VRAM).
- The RAM caches, bounded: a prompt cache of 1 GiB (llama.cpp's default is 8 GiB) for conversations that
  leave the slots, and 4 context checkpoints per slot (default 32). Checkpoints are snapshots of the state
  that models with recurrent or sliding-window layers cannot roll back token by token; without one, a
  turn whose template drops the previous turn's reasoning reprocesses the whole conversation. Some builds
  space checkpoints 8,192 tokens apart by default, which leaves short conversations without any, so
  baabaa sets 256.
- Refused extra arguments: anything that places layers, tensors, the KV cache or the encoder (`-ngl`, `-ot`,
  `-dev`, `-nkvo`, `--no-mmproj-offload`, `--cpu-moe`, flags naming `cpu`, `offload` or `draft`), the cache
  and slot settings, loading modes, and the other settings baabaa makes itself.
- Environment: only `HOME` (baabaa's `runtime/` folder, which also keeps the GPU code CUDA compiles on first
  use, `CUDA_CACHE_MAXSIZE=1 GiB`), `PATH=/usr/bin:/bin`, `LD_LIBRARY_PATH` (the server's folder and the
  registered library folders), `LLAMA_ARG_FIT=off` (fail rather than spill), `LLAMA_API_KEY` (a new random
  key each start, so other programs cannot use the server) and, for Ollama's build, `GGML_BACKEND_PATH`.
- Chat uses the server's OpenAI-compatible endpoint; replies, thinking, tool calls, images, audio and timings
  are translated to the same shape as Ollama's, so conversations, statistics and Stop work the same.
- The server's log is `$BAABAA_HOME/runtime/llama-server.log`.

## The checks

After loading, and again after the first streamed chunk of every request, from the load log (`-lv 4`) and
the process:

1. `offloaded N/M layers to GPU` reads N = M, and a GPU model buffer exists.
2. Model buffers in system memory (`CPU`, `CUDA_Host`, …) are no larger than the input lookup tables.
3. The KV cache and recurrent state are on the GPU (attention on the CPU would be CPU work); the encoder
   reports `CLIP using CUDA0 backend`.
4. The RAM caches are within their caps.
5. The process's GPU memory (NVML) is at least 97% of the weights outside the lookup tables.
6. Its system memory (anonymous plus shared/pinned) stays below the lookup tables plus 3 GiB: above that,
   something has run away.

During the request, the CPU guard watches it. A failed check stops the server and the request fails with
the reason; the fit test records it.

## Ollama models with images or audio

Ollama 0.30.7 runs every model with its own copy of llama-server and always passes `--no-mmproj-offload`:
the image/audio encoder sits in system RAM and runs on the CPU (its log: `CLIP using CPU backend`), while
`/api/ps` still reports the model 100% in VRAM.

So baabaa never sends images or audio to Ollama. For a turn that carries them, it runs the same model
files (from `/api/show`: the model and, when present, a separate encoder file) with Ollama's own
`llama-server` and CUDA library (`/usr/local/lib/ollama`), encoder on the GPU, with the settings and checks
above. The context comes from the model's fit test, leaving room for the encoder; on an out-of-memory load
it halves. Text-only requests for the same model then use that server while their prompt fits, so a
conversation with images does not swap programs every turn. Without Ollama's server program the turn is
refused with the reason.

Measured (8 GB card, 2026-09-30): qwen3.5:9b with an image, 16k context, 7.0 GiB VRAM, about 1.1 GiB of
system memory (0.53 GiB of it the lookup table), 85 tokens/s, correct description of the test image;
first turn 17 s including the load, a follow-up 0.5 s.

Ollama 0.31.2 changed this: it now puts the encoder on the GPU when there is room for it, and otherwise
still passes `--no-mmproj-offload` (seen on 0.35.0, 2026-10-01). `/api/ps` leaves the encoder out of the
model's size either way. qwen3.5:9b at 4k context: 6,454 MiB of VRAM in use against 5,236 reported (the
encoder on the GPU); qwen3.5:4b 3,926 against 2,983; nemotron-3-nano:4b, which has no encoder, 199 MiB
more than reported. qwen3.5:9b at 32k: 7,377 MiB in use, encoder on the GPU, runner RSS 1.0 GiB; at 48k:
7,655 MiB; at 64k: 7,169 MiB, encoder in system RAM, runner RSS 2.1 GiB. Since baabaa sends images only to
its own server, a text turn computes entirely on the GPU in both cases; at 64k the encoder sits unused in
RAM, as it always did on 0.30.7. The fit test therefore no longer stops at the first context that leaves
little VRAM free (a larger one can need less); only a failed load ends it.

## Fit tests after the update to Ollama 0.35.0

Measured 2026-10-01 on the 8 GB card (largest context that fits, writing speed, tool checks passed):
qwen3.5:9b 64k (see above), 69 tokens/s, 3/3; qwen3.5:4b 128k, 94, 3/3; qwen3.5:2b 256k, 117, 2/3 (it called the tool
for a sum it should have answered); nemotron-3-nano:4b 256k, 143, 2/3 (Ollama could not read its tool call
with a small web page); deepseek-r1:8b 16k, 94, 1/3; llm-jp-4.1-8b-thinking as pulled from Hugging Face 16k, 0/3 (it answered every
request with one token; see below), fixed 16k, 93, 3/3; bonsai-2-27b (llama.cpp) 16k, 42, 3/3. Image models: Z-Image-Turbo 15.6 s and
Qwen-Image-2.1 (staged) 93 s per 1024x1024 image, as before. Every model loaded 100% into VRAM.

## Harmony-format models (llm-jp-4.1-8b-thinking)

llm-jp-4.1-8b-thinking writes OpenAI's harmony format: thinking in `<|channel|>analysis<|message|>…`, the answer
in `final`, tool calls as `commentary` to `functions.NAME`. Pulled from Hugging Face (`hf.co/llm-jp/…:Q4_K_M`),
it came with an Ollama template and stop list made by Hugging Face's conversion (2026-10-02): the stop list
held `<|channel|>` and `<|message|>`, which open every reply, so each answer ended after one token; the
template had a fixed date, `<|start|>` cut to `tart|>`, and forced the answer channel. The fix is a model of
its own over the same weights (no download): a template that renders exactly as the model's own (checked
with Ollama's `_debug_render_only`, apart from JSON spacing in earlier tool calls), stops `<|return|>` and
`<|call|>` only, and `PARSER passthrough`:

```sh
# the template: a Go version of the model's chat template (see the model's GGUF for the original)
curl http://127.0.0.1:11434/api/create -d '{"model": "llm-jp-4.1:8b-thinking", "from": "hf.co/llm-jp/llm-jp-4.1-8b-thinking-gguf:Q4_K_M",
  "template": "…", "parser": "passthrough", "parameters": {"stop": ["<|return|>", "<|call|>"]}}'
```

Ollama's harmony reader (`PARSER harmony`) cannot take this model's output: its tokenizer puts a space after
each marker (`<|channel|> analysis`), and the reader then returned thinking, answer and tool call as one
block of text. With `passthrough` Ollama hands over the text and baabaa reads it (`baabaa/harmony.py`); a
model gets this treatment when its Ollama parser is `passthrough` and its template uses harmony markers.
Without a parser at all, Ollama 0.35 ignores the template and answers through a ChatML fallback.

## Ollama's own caches

Ollama passes `LLAMA_ARG_*` variables from its environment on to the llama-server it starts (verified: the
runner's environment and log). By default that server may keep a prompt cache of up to 8 GiB and up to 32
context checkpoints per conversation in system RAM: a runner grew by about 105 MiB per conversation turn
(qwen3.5:9b, measured), and a long evaluation run filled one to 8.5 GiB. Two lines in its service cap
both at normal-program scale, about 2 GB:

```ini
# sudo systemctl edit ollama, then sudo systemctl restart ollama
[Service]
Environment="LLAMA_ARG_CACHE_RAM=1024"
Environment="LLAMA_ARG_CTX_CHECKPOINTS=8"
```

Capping rather than switching them off keeps conversations from being reprocessed on the GPU at every
turn. `baabaa doctor` and Settings → Models report whether the caps are in place.

## When Ollama stops answering

Measured 2026-10-01 with Ollama 0.30.7: after it failed to parse a model's tool call mid-answer ("XML
syntax error … element <parameter> closed by </function>", nemotron-3-nano:4b), Ollama served nothing
more for that model. A 3-token request waited 25 s with the GPU at 0 % and no connection to the model's
runner; unloading the model (`keep_alive: 0`) answered in 56 ms and the next request worked. baabaa now
unloads a model after Ollama fails mid-answer, and when a request gets no answer while the GPU sits idle
for 45 s it unloads the model and asks once more; a second silence is reported as such. A generation may
otherwise take up to 15 minutes to start (long prompts, or other programs' requests queued in Ollama).

Ollama fixed the freeze (ollama#17825; the fix was merged 2026-08-19). Measured on 0.35.0 (2026-10-01): the
request after such a failure answered in 0.8 s. From 0.35.0 on, baabaa no longer unloads the model after a
failed answer (a reload costs 5–30 s); the 45-second watch stays.

The failure itself remains in 0.35.0 (ollama#18563, open): Ollama reads the tool calls of the Qwen 3.5 and
Nemotron 3 families as strict XML, and one closing tag the model leaves out ends the whole reply with
"XML syntax error". Long arguments make it likely. Six page-writing tool calls each (2026-10-01):
nemotron-3-nano:4b failed 1 (pages of 1–3k characters), qwen3.5:9b none (pages of 10–13k characters).
baabaa asks the model again, up to twice, with a note to write a long page in its reply instead of a tool
call; a complete page (or SVG image) written in a reply becomes an artifact. Replaying the conversation
where the owner met the error (nemotron-3-nano:4b, 4 runs of 2 turns, 2026-10-01): one such failure, recovered
without an error; every turn created or changed a page.

A model whose config names a newer Ollama (`"requires": "0.30.11"` for ornith:9b, read from the
registry) cannot be pulled by an older one. Model search marks such suggestions, and a failed install
says which version is needed.

## Example: a ternary 27B model

PrismML's Ternary-Bonsai-2-27B (a GGUF of 5.9 GB, Qwen3.5-27B architecture: 64 layers, full attention in
every fourth layer, 262k native context) runs only on PrismML's llama.cpp build. Its KV cache is 64 KiB
per token (16 attention layers x 4 KV heads x 256 x 2 x f16). Fit test on an 8 GB card (2026-09-30), two
slots: all 65/65 layers on the GPU; fits at 4k, 8k and 16k context (6.2, 6.4 and 6.9 GiB of VRAM), not at
32k; 42.7 tokens/s; prompt processing about 900 tokens/s. System memory about 0.7 GiB at the start
(265 MiB of it the token lookup table), levelling off at 1.5 GiB with a conversation's checkpoints (150 MiB
each). A four-turn conversation with a title request after the first turn reprocessed only each turn's
new text (0.6–1.2 s). Tool calls (the memory tool) work through the model's own chat template.

## Image models (stable-diffusion.cpp)

```sh
baabaa models add-sdcpp NAME --server /path/to/sd-server --diffusion-model MODEL.gguf \
    --llm TEXT-ENCODER.gguf --vae VAE.safetensors [--llm-vision MMPROJ.gguf --edit] \
    [--steps 8 --cfg 1.0] [--arg=--diffusion-fa --arg=--vae-tiling] [--lib /path/to/cuda/libs] [--staged]
baabaa models test NAME       # loads it, checks the rules, and times one 1024x1024 image
```

- `sd-server` must be a CUDA build. stable-diffusion.cpp publishes no Linux CUDA binaries; build it with
  `-DSD_CUDA=ON` (a user-space CUDA toolkit is enough, for example from conda-forge: `cuda-nvcc`,
  `cuda-cudart-dev`, `libcublas-dev`) and pass the toolkit's `lib` folder with `--lib`.
- Every part is computed on the GPU (`--backend CUDA0`).
- A model that fits whole keeps every part in VRAM: `--params-backend CUDA0 --auto-fit off --eager-load`.
  Newer builds' auto-fit would otherwise park the text encoder's and VAE's weights in RAM by free memory
  (2.5 GB for Z-Image-Turbo on an 8 GB card). The checks: the load log's `total params memory size = …
  (VRAM …, RAM …)` reports no weights in RAM, and the process holds at least 90% of the files in VRAM.
- **Staged** (`--staged`) is for a model too large to hold whole: each part is read from its file into
  VRAM for its step and released after (`--params-backend disk`), so system memory stays small and the
  computing stays on the GPU. Builds without that option can stage only through RAM (`--offload-to-cpu`),
  which the RAM ceiling refuses for large models.
- For every image server: system memory below 3 GiB, and the CPU guard. No conditioning cache
  (`--conditioning-cache-size 0`). Refused registered arguments: the placement flags (`--backend`,
  `--params-backend`, `--auto-fit`, `--max-vram`, `--offload-to-cpu`, `--clip-on-cpu`, `--vae-on-cpu`, …).
- Jobs use the server's job API (`/sdcpp/v1/img_gen`, polled, cancellable: Stop works).
- In a conversation, images come from the Image switch in the message box (attach an image to change it)
  or from the `generate_image` tool, which the chat model calls when asked for a picture. With more than
  one image model, a menu beside the Image switch picks which one (a setting per person, showing each
  model's time for a 1024x1024 image from its fit test); without a choice, the fastest approved model
  draws, and an edit goes to a model that can edit. Images are saved with the conversation and listed on
  the Images page.

Image models on an 8 GB card, measured 2026-09-30:

| Model | Files | How it runs | Time per image | System memory | Licence |
|---|---|---|---|---|---|
| Z-Image-Turbo | `z_image_turbo-Q4_K.gguf` 3.86 GB + `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` 2.50 GB + FLUX.1 `ae.safetensors` 0.34 GB | whole: 6.2 GB of weights in VRAM | 1024x1024 in 15.6 s, 512x512 in 4.0 s (8 steps) | 0.4–0.6 GiB | Apache-2.0 |
| Qwen-Image-2.1 | `qwen_image_2.1-Q5_0.gguf` 5.07 GB + `Qwen3VL-8B-Instruct-Q4_K_M.gguf` 5.03 GB + `mmproj-Qwen3VL-8B-Instruct-F16.gguf` 1.16 GB + `qwen_image_2.1_vae_bf16.safetensors` 0.68 GB | staged from disk: text encoder, then diffusion model, then VAE, each read into VRAM (8–9 s each from an SSD) | 1024x1024 in 94 s, 512x512 in 36 s (20 steps, CFG 6); an edit of a 1024x1024 image in 3.6 min | at most 1.45 GiB | Qwen Research (non-commercial) |
| FLUX.2 klein 4B | `flux-2-klein-4b-Q8_0.gguf` 4.30 GB + Qwen3-4B + FLUX.2 VAE | tight whole at Q8_0 (no smaller GGUF published; unmeasured) | | | Apache-2.0 |

Z-Image-Turbo draws photographic scenes and lettering cleanly and fast. Qwen-Image-2.1 follows prompts
more closely (it drew the asked-for breed), edits images, and makes transparent PNGs.

## Dictation

The microphone button records in the browser (16 kHz mono WAV, captured with an AudioWorklet; the
browser's own speech recognition is not used because some browsers send audio to a cloud service) and
`POST /api/transcribe` has an approved model that hears audio (for example `gemma4:e2b-it-qat`) write
down the words, through the same queue and checks, as a turn with audio (see "Ollama models with images or
audio"). The audio is not stored. The owner can pick the model with the `stt_model` setting; otherwise the
smallest approved audio model is used.

Measured with gemma4:e2b-it-qat (2026-09-30): eight spoken test clips transcribed correctly (a noise clip
gave empty text), about 0.1 s each once loaded; the first clip 16 s including the load. Its server holds
about 3 GiB of system memory steadily, 2.1 GiB of it gemma4's per-layer lookup tables, which the model's
design keeps outside the GPU.
