"""Detailed per-turn logging.

Two channels:
  • CONSOLE (for humans): colored real-time output, EVERYTHING in full: system
    prompt, dialogue history, recognized utterance, answer, tokens, timings.
  • FILE logs/smotri.log (for debugging): the same content in full, rotated.

Raw media (video/audio/images in base64) is never logged, only sizes.
"""

from __future__ import annotations

import itertools
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# non-ASCII/emoji in the Windows console without UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

# ── file logger (full) ───────────────────────────────────────────────────────
_file = logging.getLogger("smotri.turns")
_file.setLevel(logging.INFO)
_file.propagate = False
if not _file.handlers:
    _h = RotatingFileHandler(LOG_DIR / "smotri.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _file.addHandler(_h)

# ── ANSI colors for the console ──────────────────────────────────────────────
DIM = "2"; BOLD = "1"; CYAN = "96"; GREEN = "92"; YELLOW = "93"
BLUE = "94"; RED = "91"; MAG = "95"


def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _out(s: str = "") -> None:
    try:
        print(s, flush=True)
    except Exception:  # noqa: BLE001
        try:
            print(s.encode("ascii", "replace").decode(), flush=True)
        except Exception:  # noqa: BLE001
            pass


def _indent(text: str, code: str = DIM) -> str:
    return _c("\n".join("   " + ln for ln in (text or "").split("\n")), code)


def _hist_lines(history: list[dict]) -> list[tuple[str, str]]:
    out = []
    for m in history or []:
        role = m.get("role", "?")
        c = m.get("content")
        txt = c if isinstance(c, str) else "".join(
            b.get("text", "") for b in c if isinstance(b, dict)
        )
        out.append((role, txt))
    return out


_counter = itertools.count(1)


class TurnLog:
    def __init__(self, frame_bytes: int, audio_sec: float) -> None:
        self.n = next(_counter)
        self.t0 = time.time()
        self.first_text: float | None = None
        self.first_audio: float | None = None
        ts = time.strftime("%H:%M:%S")
        _out()
        _out(_c("━" * 64, CYAN))
        _out(_c(f"  TURN #{self.n}", f"{BOLD};{CYAN}") + _c(f"    {ts}", DIM))
        _out(_c(f"  👁  frame {frame_bytes / 1024:.0f} KB     🎤 speech {audio_sec:.1f}s", DIM))
        _out(_c("━" * 64, CYAN))
        _file.info("===== TURN #%d ===== frame=%dB audio=%.1fs", self.n, frame_bytes, audio_sec)

    def prompt(self, system: str, history: list[dict]) -> None:
        _out(_c("🧠 SYSTEM PROMPT:", YELLOW))
        _out(_indent(system))
        hl = _hist_lines(history)
        if hl:
            _out(_c(f"💬 DIALOGUE HISTORY ({len(hl)} msgs):", YELLOW))
            for role, txt in hl:
                _out(_c(f"   [{role}] ", MAG) + txt)
        else:
            _out(_c("💬 DIALOGUE HISTORY: empty (new dialogue)", DIM))
        _file.info("SYSTEM:\n%s", system)
        _file.info("HISTORY:\n%s", "\n".join(f"[{r}] {t}" for r, t in hl) or "(empty)")

    def stt(self, text: str) -> None:
        _out(_c("📝 HEARD (STT):", YELLOW) + " " + _c(text or "(empty)", BOLD))
        _file.info("STT: %s", text)

    def mark_text(self) -> None:
        if self.first_text is None:
            self.first_text = time.time() - self.t0

    def mark_audio(self) -> None:
        if self.first_audio is None:
            self.first_audio = time.time() - self.t0

    def answer(self, text: str) -> None:
        _out(_c("🗣  AGENT ANSWER:", GREEN))
        _out(_indent(text, GREEN))
        _file.info("ANSWER:\n%s", text)

    def usage(self, u: dict | None, label: str = "brain") -> None:
        if not u:
            return
        line = f"in {u.get('prompt_tokens')} | out {u.get('completion_tokens')}"
        _out(_c(f"📊 TOKENS ({label}): ", BLUE) + _c(line, DIM))
        _file.info("USAGE(%s): %s", label, line)

    def look(self, query: str, desc: str) -> None:
        _out(_c("👁  LOOK: ", MAG) + _c(query, BOLD) + _c("  → ", DIM) + desc)
        _file.info("LOOK: %s -> %s", query, desc)

    def done(self, audio_bytes: int) -> None:
        dt = time.time() - self.t0
        ft = f"{self.first_text:.2f}s" if self.first_text is not None else "—"
        fa = f"{self.first_audio:.2f}s" if self.first_audio is not None else "—"
        asec = audio_bytes / 2 / 24000
        _out(_c(f"⚡ first text {ft}  |  first audio {fa}  |  🔊 {asec:.1f}s of speech "
                f"({audio_bytes / 1024:.0f} KB)", BLUE))
        _out(_c(f"✅ turn #{self.n} done in {dt:.1f}s", f"{BOLD};{GREEN}"))
        _file.info("DONE turn#%d total=%.1fs first_text=%s first_audio=%s audio=%.1fs",
                   self.n, dt, ft, fa, asec)

    def interrupted(self) -> None:
        _out(_c(f"✋ turn #{self.n} interrupted (barge-in)", YELLOW))
        _file.info("INTERRUPTED turn#%d", self.n)

    def error(self, msg: str) -> None:
        _out(_c(f"❌ turn #{self.n}: {msg}", f"{BOLD};{RED}"))
        _file.info("ERROR turn#%d: %s", self.n, msg)


def new_turn(frame_bytes: int, audio_sec: float) -> TurnLog:
    return TurnLog(frame_bytes, audio_sec)


def event(msg: str, code: str = CYAN) -> None:
    _out(_c("• " + msg, code))
    _file.info(msg)
