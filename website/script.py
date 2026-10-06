"""What the website's preview shows: its conversations, and the replies the stand-in model gives in them.

tools/website.py plays each conversation through the real server and records what happens; the preview then
plays the recording back in the browser. Everything here was written by hand: no model wrote these replies,
and the preview says so.

Each conversation has a `prompt` (what the person asks) and `replies`, one for each time the model is called,
in the stand-in's format (tests/fixtures/fake_ollama.py). `home` marks the suggestions on the home page,
`folder` a conversation that works in the sample project below, `shown` one that is in the sidebar from the
start.
"""

PROJECT_NAME = "sheep-counter"
PROJECT = {
    "counter.py": '''"""Count the sheep in a field from the ear tags a reader picked up."""


def count(tags):
    """How many different sheep the tags name. A tag may have been read twice."""
    return len(tags)


def missing(tags, flock):
    """The sheep of the flock that no tag names, sorted."""
    return sorted(set(flock) - set(tags))
''',
    "test_counter.py": '''import unittest

from counter import count, missing


class TestCounter(unittest.TestCase):
    def test_count(self):
        self.assertEqual(count(["bo", "peep", "bo"]), 2)

    def test_missing(self):
        self.assertEqual(missing(["bo"], ["bo", "peep", "dolly"]), ["dolly", "peep"])


if __name__ == "__main__":
    unittest.main()
''',
}

SHEEP_PAGE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sheep counter</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 18px/1.5 system-ui, sans-serif;
         background: #f4f3ee; color: #1f1e1c; }
  main { text-align: center; padding: 24px; }
  .sheep { width: 132px; height: 132px; color: #2f7d6d; border: 0; background: none; cursor: pointer; padding: 0; }
  .sheep:active svg { transform: translateY(3px); }
  .count { font: 600 72px/1 ui-serif, Georgia, serif; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
  .label { color: #58544c; margin: 0 0 20px; }
  .reset { font: inherit; padding: 8px 18px; border-radius: 10px; border: 1px solid #cfc9bc; background: #fff; cursor: pointer; }
  button:focus-visible { outline: 3px solid #2f7d6d; outline-offset: 3px; border-radius: 12px; }
</style>
</head>
<body>
<main>
  <button class="sheep" id="sheep" type="button" aria-label="Count one sheep">
    <svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 50v6M44 50v6"/>
      <path fill="#fff" d="M18 30c-5 0-8 4-7 8 1 5 6 6 9 5 1 5 6 8 11 7 4 4 11 4 15 0 5 1 9-3 9-8 3-2 4-7 1-10 1-5-3-9-8-8-2-5-8-6-12-3-4-3-10-2-12 3-3-1-6 1-6 6z"/>
      <ellipse cx="17" cy="33" rx="6.5" ry="8" fill="#fff"/>
      <circle cx="15" cy="31.5" r="1.3" fill="currentColor" stroke="none"/>
      <path d="M11 28c-2-1-3-3-2-5M22 27c2-1 3-3 2-5"/>
    </svg>
  </button>
  <div class="count" id="count" aria-live="polite">0</div>
  <p class="label">sheep counted</p>
  <button class="reset" id="reset" type="button">Reset</button>
</main>
<script>
  let n = 0;
  const count = document.getElementById('count');
  function show() { count.textContent = n; }
  document.getElementById('sheep').addEventListener('click', () => { n += 1; show(); });
  document.getElementById('reset').addEventListener('click', () => { n = 0; show(); });
</script>
</body>
</html>
'''

CONVERSATIONS = [
    {
        "key": "poem", "title": "Sheep on a hill", "home": True,
        "prompt": "Write a short poem about sheep on a hill",
        "replies": [{
            "thinking": "A short poem, so two stanzas at most. Concrete pictures work better than adjectives: the hill, the wind, "
                        "one bell, evening coming on. A loose rhyme keeps it light. End quietly.",
            "content": "The hill wears its flock like a loose grey shawl\n"
                       "that the wind keeps trying to mend.\n"
                       "One bell, far off, keeps count of them all,\n"
                       "the near ones and those round the bend.\n\n"
                       "They climb where the last of the light has gone,\n"
                       "unhurried, heads low to the ground,\n"
                       "and evening comes up the slope like a yawn\n"
                       "and gathers them in without sound.",
        }],
    },
    {
        "key": "gpu", "title": "How GPUs run language models", "home": True,
        "prompt": "Explain how GPUs run language models",
        "replies": [{
            "thinking": "They want the idea, not the mathematics. Start with what a model is: a very large table of numbers. "
                        "Then what one step does, which is multiplying.\n\n"
                        "Why a GPU fits: many small multiplications at once, and memory right beside the processor. "
                        "Then what it means on their own machine: the model has to fit in the GPU's memory. "
                        "Keep the numbers rough and say that they are rough.",
            "content": "A language model is a very large table of numbers, called weights. To produce one piece of a word (a token), "
                       "the computer turns your text into numbers and multiplies them by those weights, layer after layer. A model "
                       "with 9 billion weights does on the order of ten billion multiplications for every token.\n\n"
                       "**Why a GPU**\n\n"
                       "- Those multiplications don't depend on each other, so they can run side by side. A CPU has a few "
                       "powerful cores; a GPU has thousands of simple ones, built for exactly this.\n"
                       "- The weights are read again for every token. A GPU keeps them in its own fast memory (VRAM), right "
                       "next to its cores.\n\n"
                       "**What happens when you send a message**\n\n"
                       "1. Your text is cut into tokens.\n"
                       "2. The model reads all of them once. That is the short wait before the first word.\n"
                       "3. It then predicts one token at a time, feeding each one back in, until the answer is finished.\n\n"
                       "**Why fitting in memory matters**\n\n"
                       "If the weights don't fit in VRAM, part of the work falls back to the CPU and ordinary memory, and the "
                       "speed drops from tens of tokens a second to a few. That is why baabaa only offers models that load "
                       "entirely onto your GPU.\n\n"
                       "| Model size | Memory it needs, roughly |\n|---|---|\n"
                       "| 4 billion weights | 3 to 4 GB |\n| 9 billion | 6 to 7 GB |\n| 27 billion | 17 to 20 GB |\n\n"
                       "These figures are for the compressed (4-bit) versions most people run.",
        }],
    },
    {
        "key": "dinners", "title": "A week of simple dinners", "home": True,
        "prompt": "Plan a week of simple dinners",
        "replies": [{
            "thinking": "Simple means few ingredients and half an hour at most. Vary the base through the week: pasta, rice, eggs, "
                        "soup, fish, a tray in the oven, and one evening of leftovers. A table reads best. Add a short shopping note.",
            "content": "Seven dinners, each under 30 minutes of work:\n\n"
                       "| Day | Dinner | Time |\n|---|---|---|\n"
                       "| Monday | Pasta with tomato, garlic and basil | 20 min |\n"
                       "| Tuesday | Fried rice with egg, peas and spring onion | 20 min |\n"
                       "| Wednesday | Lentil soup with carrot, and bread | 30 min |\n"
                       "| Thursday | Omelette with cheese, and a green salad | 15 min |\n"
                       "| Friday | Baked salmon, potatoes and broccoli on one tray | 30 min |\n"
                       "| Saturday | Tortilla wraps with beans, pepper and yoghurt | 20 min |\n"
                       "| Sunday | Leftovers, or the soup again with a fried egg on top | 10 min |\n\n"
                       "**One shop covers it:** pasta, rice, red lentils, tinned tomatoes, tinned beans, eggs, cheese, yoghurt, "
                       "salmon, potatoes, carrots, broccoli, peppers, spring onions, garlic, salad leaves, tortillas, bread, "
                       "frozen peas.\n\n"
                       "Cook double rice on Tuesday and double lentils on Wednesday, and Sunday takes care of itself.",
        }],
    },
    {
        "key": "tea", "title": "Flowchart of making tea", "home": True, "shown": True,
        "prompt": "Draw a flowchart of making tea",
        "replies": [{
            "thinking": "A flowchart, so a diagram and not a list. The steps: boil water, tea in the cup, pour, wait, take the bag "
                        "out, drink. Two decisions make it a real flowchart: strong enough, and milk or not.",
            "content": "```mermaid\nflowchart TD\n"
                       "    A[Fill the kettle] --> B[Boil the water]\n"
                       "    B --> C[Put a tea bag in the cup]\n"
                       "    C --> D[Pour the water over it]\n"
                       "    D --> E[Wait three minutes]\n"
                       "    E --> F{Strong enough?}\n"
                       "    F -- No --> W[Wait another minute]\n"
                       "    W --> F\n"
                       "    F -- Yes --> G[Take the bag out]\n"
                       "    G --> H{Milk?}\n"
                       "    H -- Yes --> I[Add a splash of milk]\n"
                       "    H -- No --> J[Drink it]\n"
                       "    I --> J\n```\n\n"
                       "Black tea likes water just off the boil. Green tea is better a little cooler, around 80 °C.",
        }],
    },
    {
        "key": "page", "title": "Sheep counter page", "shown": True,
        "prompt": "Make a small web page that counts sheep as I click",
        "replies": [
            {"thinking": "One page with a number and something to click. Keep it self-contained, with no libraries. "
                         "A sheep to click is friendlier than a plain button, and a Reset button starts again.",
             "tool_calls": [{"name": "create_artifact", "arguments": {"title": "Sheep counter", "kind": "html", "content": SHEEP_PAGE}}]},
            {"content": "The page is beside this chat. Click the sheep to count one; **Reset** starts again. "
                        "It is one file with no libraries, so you can save it and open it anywhere."},
        ],
    },
    {
        "key": "fix", "title": "Fix the failing test", "folder": True, "shown": True,
        "prompt": "Find and fix a failing test",
        "replies": [
            {"thinking": "First see what is in the folder.", "tool_calls": [{"name": "list_files", "arguments": {}}]},
            {"thinking": "Two files: the code and its tests. Run the tests to find the one that fails.",
             "tool_calls": [{"name": "bash", "arguments": {"command": "python3 -m unittest -q"}}]},
            {"thinking": "test_count fails: it expected 2 and got 3. The tags name the same sheep twice, so count has to ignore "
                         "repeats. Read the function before changing it.",
             "tool_calls": [{"name": "read_file", "arguments": {"path": "counter.py"}}]},
            {"thinking": "There it is: count returns len(tags), every tag, repeats included. It should count different tags.",
             "tool_calls": [{"name": "edit_file", "arguments": {"path": "counter.py", "old_text": "    return len(tags)",
                                                                  "new_text": "    return len(set(tags))"}}]},
            {"thinking": "Run the tests again to be sure.",
             "tool_calls": [{"name": "bash", "arguments": {"command": "python3 -m unittest -q"}}]},
            {"content": "The failing test was `test_count`. `count()` returned the number of tags, so a sheep whose tag was read "
                        "twice was counted twice.\n\nI changed one line in `counter.py`:\n\n```python\nreturn len(set(tags))\n```\n\n"
                        "Both tests pass now."},
        ],
    },
]

# What the stand-in model answers during the fit test: its speed test, then its three tool checks
# (baabaa/models.py SPEED_PROMPT and TOOL_CHECKS).
FIT_REPLIES = [
    {"content": "Sheep graze on the hill while the light lasts, and come down when it goes."},
    {"tool_calls": [{"name": "get_weather", "arguments": {"city": "Oslo"}}]},
    {"content": "144"},
    {"tool_calls": [{"name": "save_file", "arguments": {"path": "page.html", "content": "<!doctype html><title>Flock</title><h1>Flock</h1>"
                                                                                          "<p>Sheep graze.</p><p>Sheep rest.</p><p>Sheep wander.</p>"}}]},
]
