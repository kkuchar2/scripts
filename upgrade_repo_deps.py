#!/usr/bin/env python3
"""
Interactive repo dependency upgrader.

Place this file at:  <repo>/scripts/upgrade_repo_deps.py
Repo root (Dockerfile, package.json, etc.) is one level up from scripts/, but
you can point anywhere with --repo-root / $UPGRADE_REPO_ROOT.

Modes:
  1. Docker  — find the latest matching nginx/base image tag and update FROM.
  2. npm     — upgrade dependencies to correct, compatible versions:
                 * bump package.json ranges with npm-check-updates (peer-aware),
                 * resolve peer conflicts by upgrading the offending package,
                 * fix vulnerabilities (prefer real bumps, overrides as fallback),
                 * keep overrides MINIMAL (every override is proven necessary),
                 * verify with a clean `npm ci` + `npm audit`.
               Strict peer deps only — legacy-peer-deps is never used.

The CLI is colorized with animated progress. Disable with --no-color or NO_COLOR.
Run without flags for the interactive menu, or drive it directly, e.g.:

    upgrade_repo_deps.py --mode npm --repo-root . --yes
    upgrade_repo_deps.py --mode docker --dry-run
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Paths (mutable; finalized in main() once the repo root is known)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
PACKAGE_JSON = REPO_ROOT / "package.json"
PACKAGE_LOCK = REPO_ROOT / "package-lock.json"


def configure_paths(root: Path) -> None:
    """Point all repo-relative paths at ``root``."""
    global REPO_ROOT, DOCKERFILE, PACKAGE_JSON, PACKAGE_LOCK
    REPO_ROOT = root
    DOCKERFILE = root / "Dockerfile"
    PACKAGE_JSON = root / "package.json"
    PACKAGE_LOCK = root / "package-lock.json"


# ---------------------------------------------------------------------------
# Config (tweak here or via env if you prefer)
# ---------------------------------------------------------------------------

DOCKER_HUB_API = "https://hub.docker.com/v2/repositories/library/{image}/tags"
DOCKER_PAGE_SIZE = 100
DOCKER_MAX_PAGES = 50

NPM_MAX_ITERATIONS = 25

# Severities that make the run FAIL if still present at the end.
AUDIT_FAIL_SEVERITIES = ("critical", "high")
# Severities we still actively try to clear (best effort, won't hard-fail).
AUDIT_SOFT_SEVERITIES = ("moderate", "low")
SEVERITY_ORDER = ("critical", "high", "moderate", "low", "info")

# Resolve the dependency tree against the lockfile only during the loop (fast,
# no node_modules churn). The final verification uses a real `npm ci`.
RESOLVE_WITH_LOCK_ONLY = True

NCU_BIN = shutil.which("ncu") or "ncu"
NPM_BIN = shutil.which("npm") or "npm"

# npm must never run with legacy-peer-deps (CLI flag, env, or .npmrc).
NPM_STRICT_FLAGS = ("--no-legacy-peer-deps", "--strict-peer-deps")
FORBIDDEN_NPMRC_DIRECTIVES = frozenset({"legacy-peer-deps", "legacy_peer_deps"})

DEP_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)

FROM_RE = re.compile(
    r"^(?P<prefix>\s*FROM\s+)(?P<image>[^\s:]+)(?::(?P<tag>.+))?\s*(?P<rest>#.*)?$",
    re.IGNORECASE,
)

SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)

# Runtime toggles (finalized in main()).
VERBOSE = False


# ---------------------------------------------------------------------------
# Colors + animated progress
# ---------------------------------------------------------------------------

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "underline": "\033[4m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "gray": "\033[90m",
    "bred": "\033[91m",
    "bgreen": "\033[92m",
    "byellow": "\033[93m",
    "bblue": "\033[94m",
    "bmagenta": "\033[95m",
    "bcyan": "\033[96m",
}


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


_COLOR_ENABLED = _supports_color()


def paint(text: str, *styles: str) -> str:
    if not _COLOR_ENABLED or not styles:
        return text
    prefix = "".join(_CODES[s] for s in styles if s in _CODES)
    if not prefix:
        return text
    return f"{prefix}{text}{_CODES['reset']}"


def _term_width(default: int = 80) -> int:
    try:
        return shutil.get_terminal_size((default, 20)).columns
    except Exception:
        return default


def _restore_cursor() -> None:
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
    except Exception:
        pass


atexit.register(_restore_cursor)


class Spinner:
    """A tiny threaded braille spinner. No-ops cleanly when output isn't a TTY."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, text: str) -> None:
        self.text = text
        self.enabled = _COLOR_ENABLED and _is_tty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()

    def _draw(self, frame: str, color: str) -> None:
        width = _term_width()
        avail = max(4, width - 2)
        text = self.text if len(self.text) <= avail else self.text[: avail - 1] + "…"
        sys.stdout.write("\r\033[K" + paint(frame, color) + " " + text)
        sys.stdout.flush()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            self._draw(self.FRAMES[i % len(self.FRAMES)], "bcyan")
            i += 1
            self._stop.wait(0.08)

    def start(self) -> "Spinner":
        self._t0 = time.monotonic()
        if self.enabled:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def update(self, text: str) -> None:
        self.text = text

    def stop(self, *, ok: bool = True, suffix: str = "") -> None:
        if self.enabled:
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
            sys.stdout.write("\r\033[K\033[?25h")
            sys.stdout.flush()
        symbol = paint("✓", "bgreen", "bold") if ok else paint("✗", "bred", "bold")
        line = f"{symbol} {self.text}"
        if suffix:
            line += f"  {suffix}"
        print(line, flush=True)

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = time.monotonic() - self._t0
        self.stop(ok=exc_type is None, suffix=paint(f"{elapsed:.1f}s", "gray"))
        return False


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


@contextmanager
def spinner(text: str) -> Iterator[Spinner]:
    sp = Spinner(text).start()
    try:
        yield sp
    except BaseException:
        sp.stop(ok=False, suffix=paint(f"{time.monotonic() - sp._t0:.1f}s", "gray"))
        raise
    else:
        sp.stop(ok=True, suffix=paint(f"{time.monotonic() - sp._t0:.1f}s", "gray"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LEVELS = {
    "info": ("•", "bcyan"),
    "ok": ("✓", "bgreen"),
    "warn": ("!", "byellow"),
    "err": ("✗", "bred"),
    "step": ("▸", "bmagenta"),
    "debug": ("·", "gray"),
}


def log(msg: str, *, level: str = "info") -> None:
    symbol, color = _LEVELS.get(level, ("•", "bcyan"))
    print(f"{paint(symbol, color, 'bold')} {msg}", flush=True)


def phase(title: str) -> None:
    width = min(_term_width(), 72)
    print()
    print(paint(f"▸ {title}", "bmagenta", "bold"))
    print(paint("─" * min(width, len(title) + 2), "gray"))


def banner(repo_root: Path) -> None:
    title = "Repo Dependency Upgrader"
    box_w = max(len(title), len(str(repo_root)) + 6) + 4
    top = "╭" + "─" * (box_w - 2) + "╮"
    bot = "╰" + "─" * (box_w - 2) + "╯"
    mid1 = "│ " + title.ljust(box_w - 4) + " │"
    mid2 = "│ " + f"repo: {repo_root}".ljust(box_w - 4) + " │"
    print()
    print(paint(top, "bcyan"))
    print(paint("│", "bcyan") + paint(mid1[1:-1], "bold") + paint("│", "bcyan"))
    print(paint("│", "bcyan") + paint(mid2[1:-1], "gray") + paint("│", "bcyan"))
    print(paint(bot, "bcyan"))


def render_bar(current: int, total: int, width: int = 22) -> str:
    total = max(total, 1)
    filled = int(round(width * current / total))
    filled = max(0, min(width, filled))
    bar = paint("█" * filled, "bgreen") + paint("░" * (width - filled), "gray")
    return f"{bar} {paint(f'{current}/{total}', 'bold')}"


def die(msg: str, code: int = 1) -> None:
    log(msg, level="err")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _dump(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    if result.stderr:
        sys.stderr.write(result.stderr if result.stderr.endswith("\n") else result.stderr + "\n")
    sys.stdout.flush()
    sys.stderr.flush()


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
    label: str | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command. Shows an animated spinner unless ``quiet``."""
    if quiet:
        result = subprocess.run(
            cmd, cwd=cwd or REPO_ROOT, text=True, capture_output=capture, env=env
        )
        if VERBOSE and capture:
            _dump(result)
        if check and result.returncode != 0:
            if capture:
                _dump(result)
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result

    text = label or ("$ " + " ".join(cmd))
    start = time.monotonic()
    sp = Spinner(text).start()
    result = subprocess.run(
        cmd, cwd=cwd or REPO_ROOT, text=True, capture_output=capture, env=env
    )
    elapsed = time.monotonic() - start
    ok = result.returncode == 0
    sp.stop(ok=ok, suffix=paint(f"{elapsed:.1f}s", "gray"))
    if VERBOSE and capture:
        _dump(result)
    if check and not ok:
        if capture and not VERBOSE:
            _dump(result)
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


# ---------------------------------------------------------------------------
# Semver / JSON helpers
# ---------------------------------------------------------------------------


def parse_semver(version: str) -> tuple[int, int, int, str]:
    """Return (major, minor, patch, prerelease). Prerelease sorts before release."""
    m = SEMVER_RE.match(version.strip())
    if not m:
        return (0, 0, 0, version)
    major, minor, patch, prerelease = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    return (int(major), int(minor), int(patch), prerelease)


def semver_key(version: str) -> tuple[Any, ...]:
    major, minor, patch, prerelease = parse_semver(version)
    return (major, minor, patch, 0 if prerelease == "" else 1, prerelease)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def backup_file(path: Path) -> Path:
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


# ---------------------------------------------------------------------------
# npm environment / strict-peer guards
# ---------------------------------------------------------------------------


def build_npm_env() -> dict[str, str]:
    """Environment that cannot silently enable legacy-peer-deps."""
    env = os.environ.copy()
    env["NPM_CONFIG_LEGACY_PEER_DEPS"] = "false"
    env["npm_config_legacy_peer_deps"] = "false"
    return env


def _npmrc_legacy_peer_deps_enabled(path: Path) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower().replace("_", "-")
        if key not in FORBIDDEN_NPMRC_DIRECTIVES:
            continue
        if value.strip().lower() in {"true", "1", "yes", "on"}:
            return True
    return False


def assert_no_legacy_peer_deps() -> None:
    """Fail fast if the repo or environment would enable legacy-peer-deps."""
    npmrc = REPO_ROOT / ".npmrc"
    if _npmrc_legacy_peer_deps_enabled(npmrc):
        die(
            f"{npmrc} sets legacy-peer-deps=true. Remove it — this script only "
            "resolves peer conflicts by upgrading packages or using overrides."
        )

    result = subprocess.run(
        [NPM_BIN, "config", "get", "legacy-peer-deps"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=build_npm_env(),
    )
    value = (result.stdout or "").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        die(
            "npm legacy-peer-deps is enabled globally. Disable it before running "
            "this script (npm config set legacy-peer-deps false)."
        )


def npm_cmd(*args: str, strict: bool = True) -> list[str]:
    """Build an npm command; install/ci/audit always enforce strict peer resolution."""
    forbidden = {"--legacy-peer-deps", "--no-strict-peer-deps"}
    if any(arg in forbidden for arg in args):
        die(f"Refusing to run npm with forbidden flag(s): {', '.join(sorted(forbidden))}")
    cmd = [NPM_BIN, *args]
    if strict:
        cmd.extend(NPM_STRICT_FLAGS)
    return cmd


def run_npm(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    strict: bool = True,
    quiet: bool = False,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        npm_cmd(*args, strict=strict),
        cwd=cwd,
        check=check,
        capture=capture,
        env=build_npm_env(),
        quiet=quiet,
        label=label,
    )


# ---------------------------------------------------------------------------
# Mode 1 — Docker image upgrade
# ---------------------------------------------------------------------------


@dataclass
class DockerFromLine:
    line_index: int
    prefix: str
    image: str
    tag: str
    rest: str
    raw: str


def parse_dockerfile(path: Path) -> tuple[list[str], DockerFromLine | None]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = FROM_RE.match(line.rstrip("\n"))
        if m:
            return lines, DockerFromLine(
                line_index=i,
                prefix=m.group("prefix"),
                image=m.group("image"),
                tag=m.group("tag") or "latest",
                rest=m.group("rest") or "",
                raw=line,
            )
    return lines, None


def split_image_tag(tag: str) -> tuple[str, str]:
    """
    Split '1.30.0-alpine3.23-slim' -> ('1.30.0', 'alpine3.23-slim').
    If no semver prefix found, return ('', tag).
    """
    m = re.match(r"^(\d+\.\d+\.\d+(?:-[^-]+)?)(?:-(.+))?$", tag)
    if not m:
        return "", tag
    version, suffix = m.group(1), m.group(2) or ""
    return version, suffix


def fetch_docker_tags(image: str) -> list[str]:
    tags: list[str] = []
    url: str | None = DOCKER_HUB_API.format(image=urllib.parse.quote(image, safe=""))
    pages = 0
    with spinner(f"Fetching Docker Hub tags for {image}") as sp:
        while url and pages < DOCKER_MAX_PAGES:
            pages += 1
            sp.update(f"Fetching tags for {image} (page {pages}, {len(tags)} so far)")
            req = urllib.request.Request(
                f"{url}?page_size={DOCKER_PAGE_SIZE}",
                headers={"Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.load(resp)
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Failed to fetch Docker Hub tags for {image}: {exc}") from exc

            for row in payload.get("results", []):
                name = row.get("name")
                if name:
                    tags.append(name)
            url = payload.get("next")
    return tags


def find_latest_matching_tag(image: str, current_tag: str) -> str | None:
    current_version, current_suffix = split_image_tag(current_tag)
    try:
        all_tags = fetch_docker_tags(image)
    except RuntimeError as exc:
        die(str(exc))
        return None

    candidates: list[str] = []
    for tag in all_tags:
        if tag in ("latest",):
            continue
        version, suffix = split_image_tag(tag)
        if not version:
            continue
        if current_suffix and suffix != current_suffix:
            continue
        if not current_suffix and suffix:
            if "-" in tag and not tag.startswith(version + "-"):
                continue
        candidates.append(tag)

    if not candidates:
        return None

    def sort_key(tag: str) -> tuple[Any, ...]:
        version, _ = split_image_tag(tag)
        return semver_key(version)

    best = max(candidates, key=sort_key)
    # Never propose a downgrade.
    if current_version and sort_key(best) < semver_key(current_version):
        return current_tag
    return best


def upgrade_dockerfile(dry_run: bool = False) -> None:
    if not DOCKERFILE.is_file():
        die(f"Dockerfile not found at {DOCKERFILE}")

    lines, from_line = parse_dockerfile(DOCKERFILE)
    if not from_line:
        die("No FROM line found in Dockerfile")
    assert from_line is not None  # for type-checkers

    log(f"Current image: {paint(from_line.image + ':' + from_line.tag, 'bcyan', 'bold')}")
    latest = find_latest_matching_tag(from_line.image, from_line.tag)
    if not latest:
        die(f"Could not find any matching tags for {from_line.image} (suffix pattern preserved)")

    if latest == from_line.tag:
        log(f"Already on the latest matching tag: {paint(latest, 'bgreen', 'bold')}", level="ok")
        return

    log(f"Latest matching tag: {paint(latest, 'bgreen', 'bold')}")
    new_line = f"{from_line.prefix}{from_line.image}:{latest}"
    if from_line.rest:
        new_line += f" {from_line.rest.strip()}"
    new_line += "\n"

    if dry_run:
        log(f"DRY RUN — would write: {paint(new_line.strip(), 'dim')}", level="warn")
        return

    backup_file(DOCKERFILE)
    lines[from_line.line_index] = new_line
    DOCKERFILE.write_text("".join(lines), encoding="utf-8")
    log(
        f"Updated {DOCKERFILE.name}: "
        f"{paint(from_line.tag, 'yellow')} → {paint(latest, 'bgreen', 'bold')}",
        level="ok",
    )


# ---------------------------------------------------------------------------
# Mode 2 — npm upgrade + audit + minimal overrides
# ---------------------------------------------------------------------------


@dataclass
class AuditFinding:
    name: str
    severity: str
    vulnerable_range: str
    fix_available: bool
    fix_name: str | None = None
    fix_version: str | None = None
    fix_is_major: bool = False
    via: list[str] = field(default_factory=list)


def load_package_json() -> dict[str, Any]:
    if not PACKAGE_JSON.is_file():
        die(f"package.json not found at {PACKAGE_JSON}")
    return read_json(PACKAGE_JSON)


def save_package_json(data: dict[str, Any]) -> None:
    write_json(PACKAGE_JSON, data)


def dep_sections(pkg: dict[str, Any]) -> list[str]:
    return [k for k in DEP_SECTIONS if k in pkg and isinstance(pkg[k], dict)]


def is_direct_dep(pkg: dict[str, Any], name: str) -> str | None:
    for section in dep_sections(pkg):
        if name in pkg[section]:
            return section
    return None


def current_spec(pkg: dict[str, Any], name: str) -> str | None:
    for section in dep_sections(pkg):
        if name in pkg[section]:
            val = pkg[section][name]
            return val if isinstance(val, str) else None
    return None


def desired_spec(existing: str | None, version: str) -> str:
    """
    Build a package.json spec for ``version`` that preserves the author's
    operator style. Defaults to a caret range (the npm convention).
    """
    bare = version.lstrip("^~>=<v ").strip()
    if existing:
        existing = existing.strip()
        if existing.startswith("~"):
            return f"~{bare}"
        if existing.startswith("^"):
            return f"^{bare}"
        if re.match(r"^\d", existing):  # author pinned exactly — respect that
            return bare
    return f"^{bare}"


# ---- overrides ------------------------------------------------------------


def ensure_overrides(pkg: dict[str, Any]) -> dict[str, Any]:
    overrides = pkg.setdefault("overrides", {})
    if not isinstance(overrides, dict):
        die("package.json 'overrides' must be an object")
    return overrides


def set_override(pkg: dict[str, Any], name: str, version: str) -> bool:
    overrides = ensure_overrides(pkg)
    if overrides.get(name) == version:
        return False
    overrides[name] = version
    pkg["overrides"] = overrides
    log(f"override: {paint(name, 'bold')} → {paint(version, 'bcyan')}")
    return True


def remove_override(pkg: dict[str, Any], name: str) -> bool:
    overrides = pkg.get("overrides", {})
    if not isinstance(overrides, dict) or name not in overrides:
        return False
    del overrides[name]
    if overrides:
        pkg["overrides"] = overrides
    else:
        pkg.pop("overrides", None)
    return True


# ---- npm queries ----------------------------------------------------------


def npm_view_latest(package: str) -> str | None:
    result = run_npm(
        "view", package, "version", "--json", check=False, strict=False, quiet=True
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    try:
        parsed = json.loads(out)
        if isinstance(parsed, list):
            return str(parsed[-1]) if parsed else None
        return str(parsed)
    except json.JSONDecodeError:
        return out or None


def resolve_version_from_range(package: str, range_spec: str) -> str | None:
    """Highest published version satisfying the range (npm does the matching)."""
    result = run_npm(
        "view", f"{package}@{range_spec}", "version", "--json",
        check=False, strict=False, quiet=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout.strip())
            if isinstance(parsed, list):
                return str(parsed[-1]) if parsed else None
            return str(parsed)
        except json.JSONDecodeError:
            return result.stdout.strip()
    return npm_view_latest(package)


def version_satisfies_range(package: str, version: str, range_spec: str) -> bool:
    """True when ``version`` is among versions npm resolves for package@range."""
    result = run_npm(
        "view", f"{package}@{range_spec}", "version", "--json",
        check=False, strict=False, quiet=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        parsed = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return False
    versions = {str(v) for v in (parsed if isinstance(parsed, list) else [parsed])}
    return version in versions


# ---- npm actions ----------------------------------------------------------


def run_ncu_upgrade(target: str, peer: bool) -> dict[str, str]:
    """Bump package.json ranges. Returns {name: new_range} actually changed."""
    base: list[str]
    if shutil.which("ncu"):
        base = [NCU_BIN]
    else:
        base = ["npx", "--yes", "npm-check-updates"]

    args = base + ["-u", "--target", target, "--jsonUpgraded"]
    if peer:
        args.append("--peer")

    label = f"npm-check-updates (target={target}{', peer-aware' if peer else ''})"
    result = run(args, cwd=REPO_ROOT, check=False, label=label)
    if result.returncode != 0:
        if peer:
            log("ncu --peer failed; retrying without --peer", level="warn")
            return run_ncu_upgrade(target, peer=False)
        if not VERBOSE:
            _dump(result)
        die("npm-check-updates failed")

    out = (result.stdout or "").strip()
    try:
        upgraded = json.loads(out) if out else {}
        if not isinstance(upgraded, dict):
            upgraded = {}
    except json.JSONDecodeError:
        upgraded = {}
    return {str(k): str(v) for k, v in upgraded.items()}


def npm_install(*, lock_only: bool | None = None) -> subprocess.CompletedProcess[str]:
    lock_only = RESOLVE_WITH_LOCK_ONLY if lock_only is None else lock_only
    args = ["install"]
    if lock_only:
        args.append("--package-lock-only")
    return run_npm(*args, check=False, label="npm install")


def npm_ci() -> subprocess.CompletedProcess[str]:
    return run_npm("ci", check=False, label="npm ci (clean verification install)")


def regenerate_lockfile() -> None:
    if PACKAGE_LOCK.is_file():
        PACKAGE_LOCK.unlink()
    npm_install()


def npm_audit_json() -> dict[str, Any]:
    result = run_npm("audit", "--json", check=False, quiet=True)
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def try_audit_fix(force: bool = False) -> None:
    args = ["audit", "fix"]
    if RESOLVE_WITH_LOCK_ONLY:
        args.append("--package-lock-only")
    if force:
        args.append("--force")
    label = "npm audit fix" + (" --force" if force else "")
    run_npm(*args, check=False, label=label)


# ---- audit parsing --------------------------------------------------------


def parse_audit(report: dict[str, Any]) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    vulnerabilities = report.get("vulnerabilities") or {}
    for name, info in vulnerabilities.items():
        if not isinstance(info, dict):
            continue
        severity = str(info.get("severity", "info")).lower()
        via = info.get("via") or []
        chains: list[str] = []
        for item in via:
            if isinstance(item, str):
                chains.append(item)
            elif isinstance(item, dict) and item.get("name"):
                chains.append(str(item["name"]))

        fix_raw = info.get("fixAvailable")
        fix_available = bool(fix_raw)
        fix_name: str | None = None
        fix_version: str | None = None
        fix_is_major = False
        if isinstance(fix_raw, dict):
            fix_name = fix_raw.get("name")
            fix_version = fix_raw.get("version")
            fix_is_major = bool(fix_raw.get("isSemVerMajor"))

        findings.append(
            AuditFinding(
                name=name,
                severity=severity,
                vulnerable_range=str(info.get("range", "")),
                fix_available=fix_available,
                fix_name=str(fix_name) if fix_name else None,
                fix_version=str(fix_version) if fix_version else None,
                fix_is_major=fix_is_major,
                via=chains,
            )
        )
    return findings


def audit_counts(report: dict[str, Any], findings: list[AuditFinding]) -> dict[str, int]:
    """Prefer npm's own metadata counts; fall back to counting findings."""
    meta = ((report.get("metadata") or {}).get("vulnerabilities")) or {}
    if meta:
        return {sev: int(meta.get(sev, 0)) for sev in SEVERITY_ORDER}
    counts = {sev: 0 for sev in SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def total_vulns(counts: dict[str, int]) -> int:
    return sum(counts.get(sev, 0) for sev in SEVERITY_ORDER)


def format_audit_counts(counts: dict[str, int]) -> str:
    colors = {
        "critical": "bred",
        "high": "bred",
        "moderate": "byellow",
        "low": "bblue",
        "info": "gray",
    }
    parts = []
    for sev in SEVERITY_ORDER:
        n = counts.get(sev, 0)
        label = f"{sev}={n}"
        parts.append(paint(label, colors[sev], "bold") if n else paint(label, "gray"))
    return "  ".join(parts)


def worst_severities(findings: list[AuditFinding]) -> list[AuditFinding]:
    bad = set(AUDIT_FAIL_SEVERITIES)
    return [f for f in findings if f.severity in bad]


def soft_severities(findings: list[AuditFinding]) -> list[AuditFinding]:
    bad = set(AUDIT_SOFT_SEVERITIES)
    return [f for f in findings if f.severity in bad]


# ---- peer conflict parsing ------------------------------------------------


@dataclass
class PeerConflict:
    child: str
    required_range: str
    parent: str
    parent_version: str | None = None


# Scoped or unscoped package name
_PKG = r"(?:@[^/\s]+/[^\s@]+|[^/\s@][^\s@]*)"

ERESOLVE_RE = re.compile(
    rf"(?:Could not resolve dependency:\s+)?"
    rf"(?P<parent>{_PKG})\s+"
    rf"(?:requires|peer requires)\s+"
    rf"(?P<child>{_PKG})\s+"
    rf"(?P<range>\"[^\"]+\"|\S+)",
    re.IGNORECASE,
)

PEER_FROM_RE = re.compile(
    rf"peer (?:optional )?(?P<child>{_PKG})@(?P<range>\"[^\"]+\"|\S+)"
    rf" from (?P<parent>{_PKG})(?:@(?P<parent_ver>\S+))?",
    re.IGNORECASE,
)

FOUND_RE = re.compile(
    rf"Found: (?P<child>{_PKG})@(?P<version>\S+)",
    re.IGNORECASE,
)

LEGACY_PEER_SUGGESTION_RE = re.compile(r"legacy-peer-deps", re.IGNORECASE)

WHILE_RESOLVING_RE = re.compile(
    rf"While resolving: (?P<parent>{_PKG})@",
    re.IGNORECASE,
)


def strip_range(range_spec: str) -> str:
    s = range_spec.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def parse_peer_conflicts(output: str) -> tuple[list[PeerConflict], dict[str, str]]:
    """Parse npm ERESOLVE output into peer conflicts and installed versions."""
    conflicts: list[PeerConflict] = []
    seen: set[tuple[str, str, str]] = set()
    found_versions: dict[str, str] = {}

    for m in FOUND_RE.finditer(output):
        found_versions[m.group("child")] = m.group("version").rstrip(",")

    for m in PEER_FROM_RE.finditer(output):
        key = (m.group("child"), strip_range(m.group("range")), m.group("parent"))
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(
            PeerConflict(
                child=m.group("child"),
                required_range=strip_range(m.group("range")),
                parent=m.group("parent"),
                parent_version=m.group("parent_ver"),
            )
        )

    for m in ERESOLVE_RE.finditer(output):
        key = (m.group("child"), strip_range(m.group("range")), m.group("parent"))
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(
            PeerConflict(
                child=m.group("child"),
                required_range=strip_range(m.group("range")),
                parent=m.group("parent"),
            )
        )

    return conflicts, found_versions


def collect_packages_to_upgrade(output: str) -> list[str]:
    """Best-effort extraction of package names worth upgrading from npm output."""
    names: list[str] = []
    for pattern in (PEER_FROM_RE, WHILE_RESOLVING_RE):
        for m in pattern.finditer(output):
            for group in ("parent", "child"):
                if group in m.groupdict() and m.group(group):
                    name = m.group(group)
                    if name not in names:
                        names.append(name)
    return names


def extract_install_hints(output: str) -> list[tuple[str, str]]:
    """Return list of (package, suggested_version) from npm error output."""
    hints: list[tuple[str, str]] = []
    for m in ERESOLVE_RE.finditer(output):
        child = m.group("child")
        range_spec = strip_range(m.group("range"))
        ver = resolve_version_from_range(child, range_spec)
        if ver:
            hints.append((child, ver))
    return hints


# ---- conflict / vulnerability resolution ---------------------------------


def bump_direct_dependency(pkg: dict[str, Any], name: str, spec: str) -> bool:
    """Set a direct dependency's spec in package.json (across any section)."""
    changed = False
    for section in dep_sections(pkg):
        deps = pkg[section]
        if name in deps and deps[name] != spec:
            log(
                f"bump {section}.{paint(name, 'bold')}: "
                f"{paint(str(deps[name]), 'yellow')} → {paint(spec, 'bgreen')}"
            )
            deps[name] = spec
            changed = True
    return changed


def upgrade_package_to_latest(pkg: dict[str, Any], name: str) -> bool:
    """Bump a direct dep to ^latest, or pin a transitive override to latest."""
    latest = npm_view_latest(name)
    if not latest:
        return False
    if is_direct_dep(pkg, name):
        spec = desired_spec(current_spec(pkg, name), latest)
        return bump_direct_dependency(pkg, name, spec)
    return set_override(pkg, name, latest)


def satisfy_peer_range(pkg: dict[str, Any], child: str, range_spec: str) -> bool:
    """Pin a child to the newest version satisfying the peer range (exact = safe)."""
    version = resolve_version_from_range(child, range_spec)
    if not version:
        return False
    if is_direct_dep(pkg, child):
        # Exact pin guarantees the resolved version stays inside the peer range.
        return bump_direct_dependency(pkg, child, version)
    return set_override(pkg, child, version)


def try_resolve_peer_conflicts(pkg: dict[str, Any], output: str) -> bool:
    """
    Resolve peer dependency failures without legacy-peer-deps:
      1. Upgrade the package that declares the outdated peer range.
      2. Pin the peer child to a version satisfying the declared range.
      3. Fall back to generic ERESOLVE hints.
    """
    if LEGACY_PEER_SUGGESTION_RE.search(output):
        log(
            "npm suggested --legacy-peer-deps (ignored); fixing for real via "
            "upgrades and overrides instead",
            level="warn",
        )

    changed = False
    conflicts, found_versions = parse_peer_conflicts(output)

    for conflict in conflicts:
        log(
            f"peer conflict: {paint(conflict.parent, 'bold')} needs "
            f"{paint(conflict.child + '@' + conflict.required_range, 'bcyan')}"
        )
        # Prefer upgrading the package that declares the outdated peer range.
        if upgrade_package_to_latest(pkg, conflict.parent):
            changed = True
            continue

        found = found_versions.get(conflict.child)
        if found and version_satisfies_range(conflict.child, found, conflict.required_range):
            if upgrade_package_to_latest(pkg, conflict.parent):
                changed = True
            continue

        if found and is_direct_dep(pkg, conflict.child):
            if upgrade_package_to_latest(pkg, conflict.parent):
                changed = True
            continue

        if satisfy_peer_range(pkg, conflict.child, conflict.required_range):
            changed = True

    if not changed and conflicts:
        for conflict in conflicts:
            if satisfy_peer_range(pkg, conflict.child, conflict.required_range):
                changed = True

    if not changed:
        for name in collect_packages_to_upgrade(output):
            if upgrade_package_to_latest(pkg, name):
                changed = True

    if not changed:
        for name, version in dict(extract_install_hints(output)).items():
            if is_direct_dep(pkg, name):
                if bump_direct_dependency(pkg, name, desired_spec(current_spec(pkg, name), version)):
                    changed = True
            elif set_override(pkg, name, version):
                changed = True

    return changed


def apply_audit_fixes(pkg: dict[str, Any], findings: list[AuditFinding]) -> bool:
    """
    Clear vulnerabilities, preferring real upgrades over overrides:
      * direct dependency  -> bump its range to a safe version,
      * transitive package -> override it to a known-safe (latest) version.
    Overrides always target the *vulnerable* package itself, never npm's
    cross-package fix hint (which refers to a different dependency).
    """
    changed = False
    # Critical/high first, then moderate/low.
    ordered = worst_severities(findings) + soft_severities(findings)
    for finding in ordered:
        section = is_direct_dep(pkg, finding.name)
        if section:
            # Bump the direct dep. Prefer npm's fix version when it's for this
            # same package; otherwise use the latest published version.
            target = None
            if finding.fix_name == finding.name and finding.fix_version:
                target = finding.fix_version
            target = target or npm_view_latest(finding.name)
            if not target:
                continue
            spec = desired_spec(current_spec(pkg, finding.name), target)
            if bump_direct_dependency(pkg, finding.name, spec):
                changed = True
        else:
            # Transitive: force the vulnerable package to a safe version.
            safe = npm_view_latest(finding.name)
            if safe and set_override(pkg, finding.name, safe):
                changed = True
    return changed


def minimize_overrides(pkg: dict[str, Any], baseline_total: int) -> bool:
    """
    Remove every override that isn't actually required: drop it, re-resolve,
    and keep it removed only if the tree still installs cleanly and introduces
    no critical/high vulns (and no net-new vulnerabilities overall).
    """
    overrides = pkg.get("overrides")
    if not isinstance(overrides, dict) or not overrides:
        log("no overrides to minimize", level="ok")
        return False

    removed_any = False
    for name in sorted(overrides):
        value = overrides.get(name)
        if not isinstance(value, str):
            continue  # leave nested/object overrides untouched

        log(f"checking whether override {paint(name, 'bold')} is still required", level="step")
        if not remove_override(pkg, name):
            continue
        save_package_json(pkg)
        if PACKAGE_LOCK.is_file():
            PACKAGE_LOCK.unlink()
        install = npm_install()
        counts = _current_audit_counts()
        safe = (
            install.returncode == 0
            and not any(counts.get(sev, 0) for sev in AUDIT_FAIL_SEVERITIES)
            and total_vulns(counts) <= baseline_total
        )
        if safe:
            log(f"override {paint(name, 'bold')} was redundant — pruned", level="ok")
            removed_any = True
        else:
            ensure_overrides(pkg)[name] = value  # restore quietly
            save_package_json(pkg)
            if PACKAGE_LOCK.is_file():
                PACKAGE_LOCK.unlink()
            npm_install()
            log(f"override {paint(name, 'bold')} is required — kept")

    if not removed_any:
        log("all overrides are required (nothing to prune)", level="ok")
    return removed_any


def _current_audit_counts() -> dict[str, int]:
    report = npm_audit_json()
    return audit_counts(report, parse_audit(report))


# ---- summary --------------------------------------------------------------


def print_change_summary(original: dict[str, Any], final: dict[str, Any]) -> None:
    phase("Summary of package.json changes")

    any_change = False
    for section in DEP_SECTIONS:
        old = original.get(section, {}) or {}
        new = final.get(section, {}) or {}
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        names = sorted(set(old) | set(new))
        rows = [(n, old.get(n), new.get(n)) for n in names if old.get(n) != new.get(n)]
        if not rows:
            continue
        any_change = True
        log(paint(section, "bold"))
        for name, ov, nv in rows:
            if ov is None:
                print(f"    {paint('+', 'bgreen', 'bold')} {name} {paint(str(nv), 'bgreen')}")
            elif nv is None:
                print(f"    {paint('-', 'bred', 'bold')} {name} {paint(str(ov), 'red')}")
            else:
                print(
                    f"    {paint('~', 'byellow', 'bold')} {name} "
                    f"{paint(str(ov), 'yellow')} → {paint(str(nv), 'bgreen')}"
                )

    old_ov = original.get("overrides", {}) or {}
    new_ov = final.get("overrides", {}) or {}
    if isinstance(old_ov, dict) and isinstance(new_ov, dict) and (old_ov or new_ov):
        names = sorted(set(old_ov) | set(new_ov))
        rows = [(n, old_ov.get(n), new_ov.get(n)) for n in names if old_ov.get(n) != new_ov.get(n)]
        if rows:
            any_change = True
            log(paint("overrides", "bold"))
            for name, ov, nv in rows:
                if ov is None:
                    print(f"    {paint('+', 'bgreen', 'bold')} {name} {paint(str(nv), 'bgreen')}")
                elif nv is None:
                    print(f"    {paint('-', 'bred', 'bold')} {name} {paint(str(ov), 'red')} (pruned)")
                else:
                    print(
                        f"    {paint('~', 'byellow', 'bold')} {name} "
                        f"{paint(str(ov), 'yellow')} → {paint(str(nv), 'bgreen')}"
                    )
        kept = [n for n in new_ov if n in old_ov and old_ov[n] == new_ov[n]]
        if new_ov:
            log(f"overrides remaining: {paint(str(len(new_ov)), 'bold')} "
                f"({len(kept)} unchanged) — kept only what's required")

    if not any_change:
        log("no version changes were necessary", level="ok")


# ---- main npm flow --------------------------------------------------------


def upgrade_npm(dry_run: bool = False, target: str = "latest", peer: bool = True,
                strict_audit: bool = False) -> None:
    assert_no_legacy_peer_deps()

    original = load_package_json()

    if dry_run:
        phase("Dry run — previewing available upgrades")
        upgrades = run_ncu_preview(target, peer=False)
        if upgrades:
            log(f"{len(upgrades)} package(s) could be upgraded:")
            for name, ver in sorted(upgrades.items()):
                print(f"    {paint('~', 'byellow')} {name} → {paint(str(ver), 'bgreen')}")
        else:
            log("everything already on the requested target", level="ok")
        log("DRY RUN — no files were modified", level="warn")
        return

    backups = [backup_file(PACKAGE_JSON)]
    if PACKAGE_LOCK.is_file():
        backups.append(backup_file(PACKAGE_LOCK))
    log(f"backups: {', '.join(paint(b.name, 'dim') for b in backups)}")

    # Baseline so we can report what got fixed (full install populates
    # node_modules so peer-aware ncu and the audit have real data).
    phase("Baseline install + audit")
    baseline_install = npm_install(lock_only=False)
    baseline_counts: dict[str, int] | None = None
    baseline_total_v = 0
    if baseline_install.returncode == 0:
        baseline_counts = _current_audit_counts()
        baseline_total_v = total_vulns(baseline_counts)
        log("baseline audit: " + format_audit_counts(baseline_counts))
    else:
        log("baseline install failed (state is currently broken) — will repair", level="warn")
        baseline_total_v = 1 << 30  # don't let minimize step regress against an unknown baseline

    phase("Upgrading version ranges (npm-check-updates)")
    upgrades = run_ncu_upgrade(target, peer)
    if upgrades:
        log(f"{len(upgrades)} range(s) bumped:")
        for name, ver in sorted(upgrades.items()):
            print(f"    {paint('~', 'byellow')} {name} → {paint(str(ver), 'bgreen')}")
    else:
        log("npm-check-updates found nothing to bump", level="ok")
    regenerate_lockfile()

    phase("Resolving conflicts & vulnerabilities")
    converged = _resolve_loop(strict_audit)

    if not converged:
        die(
            f"Could not reach a clean state after {NPM_MAX_ITERATIONS} iterations "
            f"(strict peer deps, no legacy-peer-deps). Inspect {PACKAGE_JSON.name}, "
            f"overrides, and the npm output above."
        )

    phase("Minimizing overrides")
    pkg = load_package_json()
    minimize_overrides(pkg, baseline_total_v if baseline_counts is not None else 1 << 30)
    save_package_json(pkg)
    regenerate_lockfile()

    phase("Final verification (npm ci + audit)")
    ci_result = npm_ci()
    if ci_result.returncode != 0:
        die("npm ci failed after reaching a resolved state — see output above")

    report = npm_audit_json()
    findings = parse_audit(report)
    counts = audit_counts(report, findings)
    log("final audit: " + format_audit_counts(counts))

    remaining_bad = worst_severities(findings)
    remaining_soft = soft_severities(findings)
    if remaining_bad:
        die(f"{len(remaining_bad)} critical/high vulnerability(ies) could not be auto-fixed")
    if strict_audit and remaining_soft:
        die(f"{len(remaining_soft)} moderate/low vulnerability(ies) remain (--strict-audit)")

    print_change_summary(original, load_package_json())

    phase("Result")
    if total_vulns(counts) == 0:
        log("npm ci passed and audit is completely clean — zero vulnerabilities", level="ok")
    else:
        log("npm ci passed; no critical/high vulnerabilities remain", level="ok")
        if remaining_soft:
            log(
                f"{len(remaining_soft)} moderate/low finding(s) remain with no safe fix "
                f"(re-run with --strict-audit to force-fail on these)",
                level="warn",
            )
    if baseline_counts is not None:
        log(
            "vulnerabilities: "
            f"{paint(str(baseline_total_v), 'yellow')} → {paint(str(total_vulns(counts)), 'bgreen', 'bold')}"
        )


def _resolve_loop(strict_audit: bool) -> bool:
    """Iterate install → audit-fix → targeted fixes until the tree is clean."""
    force_used = False
    for iteration in range(1, NPM_MAX_ITERATIONS + 1):
        print()
        log(f"iteration {render_bar(iteration, NPM_MAX_ITERATIONS)}", level="step")

        pkg = load_package_json()  # always work from the current on-disk state

        install_result = npm_install()
        if install_result.returncode != 0:
            combined = (install_result.stdout or "") + (install_result.stderr or "")
            if try_resolve_peer_conflicts(pkg, combined):
                save_package_json(pkg)
                regenerate_lockfile()
                continue
            if not force_used:
                log("install failing; attempting npm audit fix --force", level="warn")
                try_audit_fix(force=True)
                force_used = True
                continue
            log("install still failing after force fix", level="warn")
            return False

        # Apply npm's own safe fixes first (may edit package.json directly).
        try_audit_fix(force=False)
        pkg = load_package_json()  # re-sync after audit fix mutated files

        report = npm_audit_json()
        findings = parse_audit(report)
        counts = audit_counts(report, findings)
        log("audit: " + format_audit_counts(counts))

        bad = worst_severities(findings)
        soft = soft_severities(findings)

        if not bad and not soft:
            return True  # clean (or only informational findings)

        # Clear everything fixable: critical/high first, then moderate/low.
        if apply_audit_fixes(pkg, bad + soft):
            save_package_json(pkg)
            regenerate_lockfile()
            continue
        # Nothing we changed; let npm try a forceful fix exactly once.
        if not force_used:
            log("no direct fix found; trying npm audit fix --force", level="warn")
            try_audit_fix(force=True)
            force_used = True
            continue
        # Stable: remaining findings have no safe automated fix.
        if not bad and not strict_audit:
            log("only moderate/low findings remain with no safe fix — accepting", level="warn")
            return True
        log("remaining vulnerabilities have no automated fix", level="warn")
        return False

    return False


def run_ncu_preview(target: str, peer: bool) -> dict[str, str]:
    """Like run_ncu_upgrade but read-only (no -u)."""
    base = [NCU_BIN] if shutil.which("ncu") else ["npx", "--yes", "npm-check-updates"]
    args = base + ["--target", target, "--jsonUpgraded"]
    if peer:
        args.append("--peer")
    result = run(args, cwd=REPO_ROOT, check=False, label="npm-check-updates (preview)")
    out = (result.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_repo_root(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env_value = os.environ.get("UPGRADE_REPO_ROOT")
    if env_value:
        return Path(env_value).expanduser().resolve()
    # Default contract: script lives in <repo>/scripts/.
    candidate = SCRIPT_DIR.parent
    if (candidate / "package.json").is_file() or (candidate / "Dockerfile").is_file():
        return candidate
    # Otherwise walk up from CWD looking for a repo marker.
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "package.json").is_file() or (parent / "Dockerfile").is_file():
            return parent
    return candidate


def prompt_mode() -> str:
    print()
    print(paint("How would you like to upgrade?", "bold"))
    print(f"  {paint('1', 'bcyan', 'bold')}) Docker base image  {paint('(Dockerfile FROM line)', 'gray')}")
    print(f"  {paint('2', 'bcyan', 'bold')}) npm dependencies    {paint('(ncu + audit + minimal overrides, strict peers)', 'gray')}")
    print(f"  {paint('q', 'bcyan', 'bold')}) Quit")
    print()
    while True:
        choice = input(paint("Select mode [1/2/q]: ", "bold")).strip().lower()
        if choice in {"1", "2", "q"}:
            return choice
        log("Invalid choice.", level="warn")


def prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(paint(f"{question} {suffix}: ", "bold")).strip().lower()
    if not ans:
        return default
    return ans in {"y", "yes"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="upgrade_repo_deps.py",
        description="Upgrade a repo's Docker base image or npm dependencies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mode", choices=("docker", "npm"), help="Run a mode non-interactively.")
    p.add_argument("--repo-root", help="Repo root (defaults to parent of scripts/ or $UPGRADE_REPO_ROOT).")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing files.")
    p.add_argument("--yes", "-y", action="store_true", help="Skip interactive confirmations.")
    p.add_argument("--target", choices=("latest", "minor", "patch", "newest"), default="latest",
                   help="npm-check-updates target.")
    p.add_argument("--no-peer", action="store_true", help="Disable peer-aware ncu upgrades.")
    p.add_argument("--strict-audit", action="store_true",
                   help="Fail if ANY vulnerability (incl. moderate/low) remains.")
    p.add_argument("--no-color", action="store_true", help="Disable colored output.")
    p.add_argument("--verbose", "-v", action="store_true", help="Print full command output.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    global _COLOR_ENABLED, VERBOSE

    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.no_color:
        _COLOR_ENABLED = False
    VERBOSE = args.verbose

    repo_root = resolve_repo_root(args.repo_root)
    if not repo_root.is_dir():
        die(f"Repo root does not exist: {repo_root}")
    configure_paths(repo_root)

    banner(repo_root)

    interactive = args.mode is None and sys.stdin.isatty()
    if args.mode:
        choice = "1" if args.mode == "docker" else "2"
    elif interactive:
        choice = prompt_mode()
    else:
        die("No --mode given and stdin is not a TTY. Use --mode docker|npm.")

    if choice == "q":
        log("Bye.")
        return

    dry_run = args.dry_run
    if interactive and not args.dry_run and not args.yes:
        dry_run = prompt_yes_no("Dry run only?", default=False)

    try:
        if choice == "1":
            upgrade_dockerfile(dry_run=dry_run)
        else:
            upgrade_npm(
                dry_run=dry_run,
                target=args.target,
                peer=not args.no_peer,
                strict_audit=args.strict_audit,
            )
    except KeyboardInterrupt:
        _restore_cursor()
        print()
        die("Interrupted.", code=130)


if __name__ == "__main__":
    main()
