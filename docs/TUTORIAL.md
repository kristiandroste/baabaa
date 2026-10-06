# Tutorial: from an empty machine to your first reply

This walks through everything once, in order: installing Ollama and baabaa, getting a model that fits your
graphics card, a first chat, letting baabaa work in a folder, and using it from a phone. Plan on about half an
hour, most of it waiting for a model to download. The [README](../README.md) is the reference for everything
this tutorial only touches.

Words on screen are written **in bold**. Lines in a grey box are typed into a terminal (on Linux: the
Terminal app; on a Mac: Terminal, in Applications → Utilities).

## What you need

- **A computer with a suitable graphics card (GPU).** Either Linux with an NVIDIA card, or a Mac with Apple
  silicon (an M1 or newer). On a Mac, baabaa is new: the installer, the tests and the sandbox are checked on
  macOS 15, but models on a Mac's GPU are not tested yet.
- **Enough GPU memory for a model.** Models run entirely on the GPU, so the card's memory decides which models
  you can use:

  | GPU memory | Models that fit, roughly |
  |---|---|
  | 8 GB | up to about 9 billion parameters |
  | 12 GB | up to about 14 billion |
  | 16 GB | up to about 20 billion |
  | 24 GB | up to about 30 billion |

  These are rough figures for the compressed versions most people run. baabaa tests every model on your card
  before it offers it, so you cannot pick one that does not fit.

  On Linux, `nvidia-smi` shows the card's memory. On a Mac the GPU shares the computer's memory, and about two
  thirds of it can hold models (Apple menu → About This Mac shows the memory).
- **Python 3.10 or newer.** Ubuntu 22.04 and later, Debian 12 and Fedora have it. On a Mac, install it from
  [python.org](https://www.python.org/downloads/macos/) or with `brew install python`; the `python3` that comes
  with macOS is too old.
- **An internet connection** for the two installs and for downloading models. After that baabaa works
  without one.

## 1. Install Ollama

Ollama is the program that runs the models. baabaa talks to it.

On Linux:

```sh
curl -fsSL https://ollama.com/install.sh | sh
```

On a Mac, download the Ollama app from [ollama.com/download](https://ollama.com/download) and open it once.

Check that it is there:

```sh
ollama --version
```

## 2. Install baabaa

```sh
curl -fsSL https://github.com/kristiandroste/baabaa/releases/latest/download/install.sh | sh
```

It needs no `sudo` and installs into your own home folder. It prints what it finds (Ollama, your GPU), then
asks three questions. Pressing Enter takes the answer in capitals:

1. **Let phones and other computers on your network use baabaa? [y/N]** Press Enter for No. baabaa then
   accepts this computer only. Section 7 shows how to open it to your network later.
2. **Start baabaa now? [Y/n]** Press Enter for Yes.
3. **Start baabaa whenever this computer starts? [y/N]** Your choice. You can change it later with
   `baabaa autostart on` or `off`.

If the installer says that `~/.local/bin` **is not on your PATH yet**, run the line it prints and open a new
terminal, so that the `baabaa` command is found.

When it has started, it prints:

```
baabaa is running on this computer only: http://localhost:8443/
```

## 3. Open it and create your account

Open **http://localhost:8443/** in a browser on the same computer. The first time, it shows **Welcome to
baabaa** and asks for:

- **Your name**: how baabaa greets you.
- **Account name (lowercase)**: a short name for signing in, `owner` unless you change it.
- **Password (optional)**: leave it empty if only you use this computer. Set one before you open baabaa to
  your network: an account without a password can be opened by anyone who can reach baabaa.

Press **Create owner account**. This first account is the owner: it chooses the models, manages other
accounts and sees everyone's usage.

## 4. Get your first model

The home page shows a card, **Pick your first model**. Press **Find a model**. baabaa opens **Settings →
Models**, searches the Ollama library and suggests up to five models that fit your GPU.

1. Press **Install** beside one of the suggestions. If you are unsure, take the first. The download runs in the
   background and shows under **In progress**; a model is several gigabytes, so this is the long wait.
2. When the download is done, the **GPU fit test** starts by itself. It loads the model with larger and larger
   context sizes (how much text the model can keep in view) and keeps the largest that fits entirely in GPU
   memory. It also measures the speed and checks how well the model uses tools.
3. The model now stands under **Installed models** with a line such as *Fits up to 32k context · 41 tokens/s ·
   tool use 3/3*. Turn on its **Approve** switch.

Only models that pass the test and that you approve can be chosen for a conversation. A model that is too
large for the card is never offered: if part of a model ran on the processor instead of the GPU, the computer
would crawl and run hot, and baabaa exists to prevent exactly that.

Close Settings. The button at the right of the message box now shows your model.

## 5. Your first chat

Click one of the suggestions under the message box, for example **Draw a flowchart of making tea**. It fills
the box; press **Enter** to send it.

While baabaa works, a thread of wool runs from the sheep under the reply:

- It **hangs slack** while the reply waits for the GPU or the model is loading. The first message after a
  pause takes longest, because the model has to be loaded into GPU memory.
- It **curls** while the model thinks or writes. Faster models make bigger curls.
- It **sags** if no words arrive for a moment.

When a thought is finished it folds into a small ball of yarn with its duration, such as **Thought for 4 s**.
Click it to read what the model was thinking. The square button, or **Esc**, stops a reply at any time.

Under each reply are buttons to copy it, try again, save it as an image, read it aloud and rate it.

Things to try in a chat:

- **Think** (a switch in the message box, for models that can): the model reasons before it answers. Slower,
  often better.
- **Research**: baabaa searches the web, reads several sources and writes a report with numbered citations.
- **Ask for something to keep**: "Make a small web page that counts sheep as I click." Pages, documents,
  diagrams and code files open beside the chat as *artifacts*, with a preview, the code, and a download.
- **Upload a file** with the **+** button: text, code, PDF, Office documents, images.

Every conversation is kept in the list at the left. **Search chats** (or Ctrl+K) searches all of them.

## 6. Let baabaa work in a folder

Give a conversation a folder and the model can read the files there, change them and run commands: fix a
bug, write a script, tidy a set of documents.

1. Press **Code** in the sidebar. A window asks you to **Choose a working folder**. Pick one and press **Use
   this folder**. Start with a folder you can afford to experiment in.
2. The first time, baabaa asks **Trust this folder?** Trusting lets it read the folder's own instructions
   (a file named `BAABAA.md` or `AGENTS.md`). Say **Not now** unless you know what is in the folder.
3. Describe the task, for example **Explain how this project is organized**.

What the model may do without asking is set by the **mode**, shown beside the message box (Shift+Tab changes
it):

| Mode | What happens |
|---|---|
| **Manual** | It asks before every edit and command. |
| **Accept edits** | Edits inside the folder run; commands ask. This is where a folder starts. |
| **Plan** | It only reads, then proposes a plan for you to approve. |
| **Auto** | Safe actions run; risky ones ask. A model judges each action, so this needs a capable model. |

When baabaa asks, you see what it wants to run and three answers: **Allow once**, **Always allow** (adds a
rule, so the same kind of command will not ask again) and **Deny**. You can also tell it what to do instead.
In a folder it also asks before reading a web page from a site it has not read from before; **Always allow**
adds the site. Only Auto mode decides that by itself.

Two things protect you while it works. Every command runs in a sandbox: it can change only the working
folder, and it has no internet unless you turn on **Network** for the conversation. And baabaa keeps
checkpoints of the files it changes: **Rewind to here**, under any of your messages, puts the files and the
conversation back to that point.

## 7. The terminal

The same conversations are available without a browser. In any folder:

```sh
cd ~/projects/something
baabaa
```

This starts a conversation that works in that folder (`baabaa chat --no-folder` for a plain chat). Type a
message and press Enter. `/help` lists the commands; the most useful are `/model`, `/mode`, `/resume` (open an
earlier conversation) and `/quit`. Esc stops a reply, Shift+Tab changes the mode, and Ctrl+O shows the model's
thinking and the full output of tools. A conversation begun here appears in the browser too, and the other
way round.

## 8. Use it from your phone or another computer

So far only this computer can reach baabaa. To use it from other devices on your home or office network:

1. **Give every account a password first** (**Settings → General → Password**). Without one, anyone on your
   network could open that account.
2. Allow other devices:

   ```sh
   baabaa network lan
   ```

   baabaa restarts by itself, or tells you to run `baabaa restart`. The same choice is in **Settings → About →
   Network**.
3. Find the address with `baabaa status`. It now starts with `https://` and contains this computer's
   address on your network, for example `https://192.168.1.20:8443/`.
4. On each device, install baabaa's certificate once: open that address followed by `ca.crt`
   (`https://192.168.1.20:8443/ca.crt`) and install the file as a trusted certificate. Without it the browser
   warns about the connection and refuses the microphone. On a phone, the setting is found by searching the
   settings for "certificate".
5. Open the address and sign in.

baabaa accepts only devices on the same network as this computer. It is not reachable from the internet, and
it should not be put there.

To share baabaa with family or colleagues, add accounts in **Settings → Accounts**. Each account has its own
conversations, files and settings, and uses only the folders you grant it.

## 9. Keeping it up to date

```sh
baabaa status      # is it running, where to open it, what the GPU is doing
baabaa update      # install the newest release
baabaa stop        # stop it; baabaa start starts it again
```

baabaa checks once a day whether a new release exists and shows a notice in the sidebar, with an **Install**
or **Restart** button. A restart waits until the replies being written are finished.

To remove it: `baabaa uninstall` removes the program and keeps your conversations; `baabaa uninstall --purge`
removes them too, after asking. Neither touches Ollama or your models.

## When something goes wrong

Start with the check-up, which names what is missing:

```sh
baabaa doctor
```

| What you see | What it means | What to do |
|---|---|---|
| `baabaa: command not found` | The terminal does not look in `~/.local/bin`. | Run the PATH line the installer printed, then open a new terminal. |
| **baabaa is not reachable. Is the server running?** | The server is stopped. | `baabaa start`. If it cannot start, it prints the reason. |
| **Ollama: not reachable** (Settings → About, or `NO  Ollama` in `baabaa doctor`) | Ollama is not running. | Linux: `sudo systemctl start ollama`. Mac: open the Ollama app. |
| **No model is ready yet.** | No model is approved. | Section 4: install one, wait for the fit test, turn on **Approve**. |
| **Larger than GPU memory: not allowed** or **Does not fit in GPU memory** | The model is too big for your card. | Choose a smaller one; **Find models** only suggests models that fit. |
| **Waiting for the GPU (1 ahead)** | Another reply is being written. One request uses the GPU at a time. | Wait, or stop the other reply. |
| The thread sags and nothing arrives for a long time | The model is loading, or writing a long action that only shows when it is complete. | Wait a little. **Esc** stops it; a smaller model answers sooner. |
| **Stopped: the model kept repeating an action that fails.** | The model is too small for the task. | Rephrase in smaller steps, or switch to a larger model. |
| A command shows **Failed**, `exit 126` | The sandbox is not available, so baabaa refused to run the command. | `baabaa doctor --sandbox` says what is missing (on Linux, a kernel older than 5.13). |
| The browser warns about the certificate on another device | The device does not know baabaa's certificate. | Section 8, step 4. |

## Where to go from here

- **Projects** (sidebar): conversations that share instructions and documents, for a subject you keep
  coming back to.
- **Memory** (Settings → Memory): things baabaa should remember about you across conversations. You can read,
  edit and delete every one of them.
- **Scheduled** (sidebar): a prompt that runs by itself, every morning for example.
- **Settings → Permissions**: the rules for what may run without asking.
- The [README](../README.md) describes every feature and command. If you want to change baabaa itself, the
  [developer guide](DEVELOPING.md) explains how it is built.
