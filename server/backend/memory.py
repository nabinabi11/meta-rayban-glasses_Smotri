"""memory.py: the agent's persistent file memory (workspace) + a file map.

The agent reads and writes files about the user itself (profile, notes, skills,
progress on a topic). The system prompt gets a MAP (profile + file tree with titles);
the agent reads specific files on demand with a tool.

The workspace LIVES OUTSIDE the code (so redeploys don't wipe it):
    MEMORY_DIR (env) | default <repo parent>/smotri-memory (Docker: /app/data/memory)
"""

from __future__ import annotations

import os
from pathlib import Path

from backend import config

_ROOT_DEFAULT = config.ROOT.parent / "smotri-memory"
MAX_READ = 16000   # read limit per file (characters)


def root() -> Path:
    p = Path(os.getenv("MEMORY_DIR") or _ROOT_DEFAULT)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(path: str) -> Path:
    """A path inside the workspace, with no way out (sandbox)."""
    base = root().resolve()
    target = (base / path.strip().lstrip("/")).resolve()
    if base != target and base not in target.parents:
        raise ValueError("path outside the workspace")
    return target


def ensure_profile() -> Path:
    p = root() / "profile.md"
    if not p.exists():
        p.write_text("# User profile\n\n(empty: run onboarding)\n", encoding="utf-8")
    return p


_LANGUAGE_SKILL = """# Skill: language practice

Method (follow it and keep the files):
- Keep the target language, level and goal in `language/progress.md` (level, current focus, mastered).
- New words and phrases go to `language/vocab.md` (word: translation: example). Come back to them.
- Practice in short voice sessions: speak the target language with the user, correct mistakes
  gently (praise first, then fix), explain in the user's native language when they're stuck.
- Get fresh examples, texts and news in simple language via web_search.
- At the end of a session update progress.md (what was covered) and vocab.md (new words).
- Adapt to the user's interests from the profile (topics they care about).
"""


def ensure_seeds() -> None:
    """Seed skills (created once if missing)."""
    sk = root() / "skills" / "language-practice.md"
    if not sk.exists():
        sk.parent.mkdir(parents=True, exist_ok=True)
        sk.write_text(_LANGUAGE_SKILL, encoding="utf-8")


# ── agent tools ──────────────────────────────────────────────────────────────
def read_file(path: str) -> str:
    try:
        t = _safe(path)
        if not t.exists():
            return f"(no such file: {path})"
        return t.read_text(encoding="utf-8")[:MAX_READ]
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def write_file(path: str, content: str) -> str:
    try:
        t = _safe(path)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(content, encoding="utf-8")
        return f"written: {path}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def append_file(path: str, text: str) -> str:
    try:
        t = _safe(path)
        t.parent.mkdir(parents=True, exist_ok=True)
        with t.open("a", encoding="utf-8") as f:
            f.write(("" if not t.exists() else "") + text.rstrip() + "\n")
        return f"appended to {path}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def remember(text: str) -> str:
    """Quickly add a fact about the user to the profile."""
    if not (text or "").strip():
        # some models call remember with empty arguments; make them retry instead of
        # writing a blank "-" line into the profile
        return "ERROR: nothing to remember. Call remember again with the fact in the 'text' argument."
    return append_file("profile.md", "- " + text.strip())


# ── map injected into the prompt ─────────────────────────────────────────────
def _title(p: Path) -> str:
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s:
                return s.lstrip("# ").strip()[:80]
    except Exception:  # noqa: BLE001
        pass
    return ""


def build_map() -> str:
    """Profile (in full) + file tree with titles, for the system prompt."""
    ensure_profile()
    ensure_seeds()
    base = root()
    profile = (base / "profile.md").read_text(encoding="utf-8")[:4000]
    files = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.name != "profile.md":
            rel = p.relative_to(base).as_posix()
            ttl = _title(p) if p.suffix == ".md" else ""
            files.append(f"  - {rel}" + (f" — {ttl}" if ttl else ""))
    tree = "\n".join(files) if files else "  (empty so far)"
    return f"## USER PROFILE\n{profile}\n\n## MEMORY FILES (read the one you need with read_file)\n{tree}"
