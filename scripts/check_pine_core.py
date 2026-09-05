"""Prove the shared core of the two XAUUSD short-scalper Pine files is identical.

The indicator (pine/xauusd_short_scalper.pine) and the strategy
(pine/xauusd_short_scalper_strategy.pine) must run the same signal logic.  Pine
has no include mechanism, so the core lives in both files between the markers
``// ===================== CORE BEGIN`` and ``// ===================== CORE END``.
This script fails (exit code 1) when the two copies differ, and also performs a
few static checks that the non-repainting contract documented in
pine/XAUUSD_SHORT_SCALPER.md still holds.

Usage:  python scripts/check_pine_core.py
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    ROOT / "pine" / "xauusd_short_scalper.pine",
    ROOT / "pine" / "xauusd_short_scalper_strategy.pine",
]
BEGIN = "// ===================== CORE BEGIN"
END = "// ===================== CORE END"
FORBIDDEN = {
    "varip": "varip would make realtime and historical bars differ",
    "barstate.isrealtime": "realtime-only branches break the historical == realtime rule",
}
# request.security() is allowed ONLY in TradingView's documented non-repainting
# form: the expression offset by [1] together with lookahead_on, which returns
# the last CLOSED higher-timeframe bar on historical and realtime bars alike.
SECURITY_IDIOM = re.compile(r"request\.security\([^\n]*\[1\],\s*lookahead\s*=\s*barmerge\.lookahead_on\)")


def core_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < 0 or stop < start:
        sys.exit(f"{path}: CORE BEGIN/END markers not found")
    return text[start:stop]


def main() -> int:
    cores = {p: core_of(p) for p in FILES}
    ok = True
    a, b = FILES
    if cores[a] != cores[b]:
        ok = False
        print("CORE MISMATCH between the indicator and the strategy:")
        for line in difflib.unified_diff(
            cores[a].splitlines(), cores[b].splitlines(), str(a.name), str(b.name), lineterm=""
        ):
            print(line)
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        code_only = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
        for needle, why in FORBIDDEN.items():
            if needle in code_only:
                ok = False
                print(f"{path.name}: contains '{needle}' -- {why}")
        for lineno, line in enumerate(code_only.splitlines(), 1):
            if "request.security" in line and not SECURITY_IDIOM.search(line):
                ok = False
                print(f"{path.name}:{lineno}: request.security() without the [1] + lookahead_on non-repainting idiom")
            if "lookahead" in line and "request.security" not in line:
                ok = False
                print(f"{path.name}:{lineno}: stray lookahead usage")
        if not re.search(r"^//@version=6", text, re.M):
            ok = False
            print(f"{path.name}: missing //@version=6 header")
        # Every signal-producing line must be gated on the confirmed bar.
        if "shortRaw  = barClosed and" not in text:
            ok = False
            print(f"{path.name}: shortRaw is no longer gated on barClosed (barstate.isconfirmed)")
        # Pine line continuations: a wrapped line must not sit on a multiple-of-4 indent.
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if not stripped or stripped.startswith("//"):
                continue
            prev = text.splitlines()[lineno - 2] if lineno > 1 else ""
            continuation = prev.rstrip().endswith((",", "+", "and", "or", ":", "?", "(")) and not prev.strip().startswith("//")
            if continuation and indent % 4 == 0 and indent > 0 and not prev.rstrip().endswith("=>"):
                # A body line following "if ...:" style openers does not exist in Pine, so
                # a multiple-of-4 indent after a dangling operator is always a wrap error.
                ok = False
                print(f"{path.name}:{lineno}: continuation line indented by {indent} (multiple of 4)")
    print("core identical, static checks passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
