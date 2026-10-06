"""The thinking thread in the terminal: one line of text for what baabaa is doing with a reply right now.

The browser draws the same thing as a strand of wool (web/js/thread.js, which this follows). A new cell
enters at the left ten times a second: a loop when text arrived in that tenth of a second, a dash when none
did or when a sentence ended. While nothing arrives the thread hangs slack; while a tool runs a bead goes
to and fro; a finished thought leaves a knot, bigger for a longer thought.
"""

import re
import time

CELLS = 22
TICK_S = 0.1
QUIET_S = 1.4  # this long without text and a thread that was running goes slack
UTF8 = {"mark": "✻", "loop": "ℓ", "line": "─", "slack": "╌", "ripple": "~", "bead": "●", "knots": "·•●"}
ASCII = {"mark": "*", "loop": "e", "line": "-", "slack": ".", "ripple": "~", "bead": "o", "knots": ".oO"}
SENTENCE_END = re.compile(r"""[.!?]["')\]]?(\s|$)""")
DOING = {
    "bash": "Running a command", "bash_output": "Checking a command", "kill_shell": "Stopping a command",
    "web_search": "Searching the web", "web_fetch": "Reading a web page", "read_file": "Reading a file",
    "list_files": "Looking at the files", "search": "Searching the files", "write_file": "Writing a file",
    "create_file": "Writing a file", "edit_file": "Editing a file", "task": "A helper is working",
    "generate_image": "Making an image", "search_project": "Searching the project", "memory": "Updating memory",
    "past_chats": "Looking at earlier chats", "create_artifact": "Making an artifact",
    "update_artifact": "Updating the artifact", "rewrite_artifact": "Updating the artifact",
    "read_artifact": "Reading the artifact", "research_plan": "Planning the research",
    "research_notes": "Taking notes", "todo_write": "Updating the task list",
}


def glyphs(encoding: str | None) -> dict:
    """The characters to draw with: plain ASCII when the terminal is not UTF-8."""
    return UTF8 if "utf" in (encoding or "").lower() else ASCII


def span(seconds: float) -> str:
    s = max(1, round(seconds))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60} s"


def live_mode(message: dict, turn: dict | None) -> str:
    """What the thread shows for a reply that is being written: wait, think, tool or write."""
    blocks = message.get("blocks") or []
    b = blocks[-1] if blocks else None
    if (turn and turn.get("queue_position")) or not b:
        return "wait"
    kind = b.get("type")
    if kind == "thinking":
        return "think" if b.get("ms") is None else "wait"  # a finished thought: the next step is being prepared
    if kind == "text":
        return "write"
    if kind in ("tool", "image"):
        return "tool" if b.get("status") in ("pending", "checking", "running") else "wait"
    return "wait"


def live_label(message: dict, turn: dict | None, line: "ThreadLine") -> str:
    """The words beside the thread."""
    blocks = message.get("blocks") or []
    b = blocks[-1] if blocks else {}
    quiet = f"quiet for {span(line.quiet)}" if line.quiet >= 3 else ""
    if line.mode == "wait":
        return f"Waiting for the GPU ({turn['queue_position']} ahead)" if turn and turn.get("queue_position") else ""
    if line.mode == "think":
        return f"Thinking… {span(line.seconds)}" + (f" · {quiet}" if quiet else "")
    if line.mode == "tool":
        return (DOING["generate_image"] if b.get("type") == "image" else DOING.get(b.get("name"), "Using a tool")) + "…"
    if line.mode == "write":
        return quiet.capitalize()
    return ""


def thought_label(block: dict) -> str:
    """The words beside a finished thought. The server records how long a thought took (`ms`) since 0.9.3."""
    return "Thoughts" if block.get("ms") is None else f"Thought for {span(block['ms'] / 1000)}"


def knot(tokens: float, g: dict) -> str:
    """A thought's ball of yarn, as far as one character can show it."""
    return g["knots"][0 if tokens <= 60 else 1 if tokens <= 600 else 2]


class ThreadLine:
    def __init__(self, g: dict = UTF8, clock=time.monotonic):
        self.g, self.clock = g, clock
        self.mode = "off"
        self.cells = [g["line"]] * CELLS
        self.tokens = 0.0
        self.began = self.last_pulse = self.ticked = clock()
        self.hit = self.gap = False

    def set(self, mode: str) -> None:
        """off, wait, think, tool or write."""
        if mode == self.mode:
            return
        if mode == "think":
            self.tokens, self.began = 0.0, self.clock()
        self.mode = mode
        self.last_pulse = self.clock()

    def pulse(self, text: str) -> None:
        """A piece of text arrived from the model."""
        self.hit = True
        if self.mode == "think":
            self.tokens += max(len(text), 1) / 4
        if "\n" in text or SENTENCE_END.search(text):
            self.gap = True
        self.last_pulse = self.clock()

    @property
    def seconds(self) -> float:
        return self.clock() - self.began

    @property
    def quiet(self) -> float:
        return self.clock() - self.last_pulse

    def tick(self) -> None:
        """Move on to the present: a new cell for every tenth of a second that has passed."""
        now = self.clock()
        self.ticked = max(self.ticked, now - CELLS * TICK_S)
        while now - self.ticked >= TICK_S:
            self.ticked += TICK_S
            busy = self.mode in ("think", "write") and self.hit and not self.gap
            cell = self.g["loop" if self.mode == "think" else "ripple"] if busy else self.g["line"]
            self.cells = [cell] + self.cells[:-1]
            self.hit = self.gap = False

    def text(self) -> str:
        g, mode = self.g, self.mode
        cells = list(self.cells)
        if mode in ("wait", "off"):
            cells = [g["slack"]] * CELLS
        elif mode == "tool":  # the thread holds still and a bead goes to and fro
            phase = (self.clock() % 1.7) / 1.7
            cells = [g["line"]] * CELLS
            cells[round(2 + (CELLS - 5) * (1 - abs(2 * phase - 1)))] = g["bead"]
        elif self.quiet >= QUIET_S:  # stalled: what is left of the thread hangs slack
            cells = [g["slack"] if c == g["line"] else c for c in cells]
        end = knot(self.tokens, g) if mode == "think" else "" if mode == "write" else g["knots"][0]
        return "".join(cells) + end
