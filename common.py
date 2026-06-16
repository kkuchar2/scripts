#!/usr/bin/env python3
"""
Shared UI primitives for galeria-weselna tooling scripts.

Import from any sibling script:
    from common import c, header, section, ok, warn, err, fail, Menu, confirm
"""

import sys

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RED     = "\033[91m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
WHITE   = "\033[97m"


def c(text: str, *codes: str) -> str:
    """Wrap text in one or more ANSI codes, reset at the end."""
    return "".join(codes) + str(text) + RESET


# ── Status helpers ─────────────────────────────────────────────────────────────
def header(title: str) -> None:
    width = 62
    print()
    print(c("╔" + "═" * width + "╗", BOLD + CYAN))
    print(c("║" + f"  {title}".ljust(width) + "║", BOLD + CYAN))
    print(c("╚" + "═" * width + "╝", BOLD + CYAN))


def section(title: str) -> None:
    print()
    print(c(f"  ┌─ {title} ", BOLD + BLUE) + c("─" * max(0, 56 - len(title)), BLUE))


def ok(msg: str) -> None:
    print(c(f"  ✔  {msg}", GREEN))


def warn(msg: str) -> None:
    print(c(f"  ⚠  {msg}", YELLOW))


def err(msg: str) -> None:
    print(c(f"  ✖  {msg}", BOLD + RED))


def fail(msg: str) -> None:
    err(msg)
    sys.exit(1)


def spinner_step(msg: str) -> None:
    print(c(f"  ›  {msg}", DIM + WHITE), end="", flush=True)


def spinner_done() -> None:
    print(c("  done", GREEN))


# ── Formatting ─────────────────────────────────────────────────────────────────
def fmt_size(size_bytes: int) -> str:
    """Human-readable byte size (matches Docker Hub dashboard style)."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


# ── Interactive input ──────────────────────────────────────────────────────────
def confirm(prompt: str = "") -> bool:
    """Ask the user to type 'yes'. Returns True only on exact 'yes'."""
    prefix = c(f"\n  {prompt}  Type ", WHITE) if prompt else c("\n  Type ", WHITE)
    return input(prefix + c("yes", BOLD + YELLOW) + c(" to confirm: ", WHITE)).strip() == "yes"


def menu_prompt(label: str = "Select option") -> str:
    """Read a menu choice from stdin and return it lowercased."""
    return input(c(f"\n  {label}: ", BOLD + WHITE)).strip().lower()


def choose(
    options: list[tuple[str, str, str]],
    title: str = "Select",
    subtitle: str | None = None,
    prompt: str = "Select option",
    label_w: int = 30,
    menu: "Menu | None" = None,
) -> str:
    """
    Draw a bordered menu box and return the user's selection key.

    options:  list of (key, label, desc) tuples — same format as Menu.row()
    subtitle: optional info line shown between title and menu rows
    label_w:  column width for the label column (default 30)
    menu:     Menu instance to use; creates Menu() if not provided
    """
    m = menu or Menu()
    print()
    print(m.top())
    print(m.title(title))
    print(m.div())
    if subtitle is not None:
        print(m.info(subtitle))
        print(m.div())
    for key, label, desc in options:
        print(m.row(key, label, desc, label_w=label_w))
    print(m.bot())
    return menu_prompt(prompt)


# ── Menu box ───────────────────────────────────────────────────────────────────
class Menu:
    """
    Builds a fixed-width bordered menu box with consistent cell padding.

    Usage:
        m = Menu(inner=68)
        print(m.top())
        print(m.title("My Tool"))
        print(m.div())
        print(m.row("1", "Do something", "description text"))
        print(m.bot())
    """

    def __init__(self, inner: int = 68) -> None:
        self.inner = inner

    def top(self) -> str:
        return c("  ╔" + "═" * self.inner + "╗", BOLD + MAGENTA)

    def bot(self) -> str:
        return c("  ╚" + "═" * self.inner + "╝", BOLD + MAGENTA)

    def div(self) -> str:
        return c("  ╠" + "═" * self.inner + "╣", BOLD + MAGENTA)

    def title(self, text: str) -> str:
        content = f"  {text}".ljust(self.inner)
        return c("  ║", BOLD + MAGENTA) + c(content, BOLD + WHITE) + c("║", BOLD + MAGENTA)

    def info(self, text: str) -> str:
        content = f"  {text}".ljust(self.inner)
        return c("  ║", BOLD + MAGENTA) + c(content, DIM) + c("║", BOLD + MAGENTA)

    def row(self, key: str, label: str, desc: str, label_w: int = 30) -> str:
        key_vis   = f" [{key}]"
        label_vis = f"  {label:<{label_w}}"
        avail     = self.inner - len(key_vis) - len(label_vis)
        desc_vis  = desc[:avail].ljust(avail)
        return (
            c("  ║", BOLD + MAGENTA)
            + c(key_vis, BOLD + CYAN)
            + c(label_vis, WHITE)
            + c(desc_vis, DIM)
            + c("║", BOLD + MAGENTA)
        )
