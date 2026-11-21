"""Console output and prompts. Kept in one place so tests can replace `ask`."""

import sys
import time
from datetime import datetime

WIDTH = 65


def _supports_unicode() -> bool:
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


BAR_FULL, BAR_EMPTY = ("█", "░") if _supports_unicode() else ("#", "-")
ARROW = "→" if _supports_unicode() else "->"


def header(title: str, char: str = "=") -> None:
    print("\n" + char * WIDTH)
    print(f"  {title}")
    print(char * WIDTH)


def info(msg: str = "") -> None:
    print(f"  {msg}" if msg else "")


def warn(msg: str) -> None:
    lines = msg.split("\n")
    print(f"\n  !! {lines[0]}")
    for line in lines[1:]:
        print(f"     {line}")


def ask(prompt: str) -> str:
    return input(f"  {prompt}").strip()


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = "(Y/n)" if default else "(y/N)"
    answer = ask(f"{prompt} {suffix}: ").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def pause() -> None:
    try:
        ask("Press Enter to continue...")
    except EOFError:
        pass


def format_size(n: float) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.2f} {unit}" if unit == "GB" else f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Progress:
    """Single-line progress bar, by bytes."""

    INTERVAL = 0.5  # seconds between redraws

    def __init__(self, label: str, total_bytes: int, width: int = 32):
        self.label, self.total, self.width, self.done = label, max(total_bytes, 1), width, 0
        self._last_draw = 0.0
        self._drawn = False

    def _draw(self) -> None:
        pct = min(self.done / self.total, 1.0)
        filled = int(self.width * pct)
        bar = BAR_FULL * filled + BAR_EMPTY * (self.width - filled)
        print(f"\r  {self.label} [{bar}] {pct * 100:5.1f}%  "
              f"{format_size(self.done)}/{format_size(self.total)}   ", end="", flush=True)
        self._drawn = True

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last_draw >= self.INTERVAL:
            self._last_draw = now
            self._draw()

    def finish(self) -> None:
        if self.done:
            self._draw()  # always end on the final state
        if self._drawn:
            print()
