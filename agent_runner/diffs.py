"""Bound a git patch before it is handed to an agent CLI.

A published PR diff is unbounded. One rebuild of a committed minified bundle
adds ~1.3 MB of single-line JavaScript, and `gh pr diff` renders it in full:
GitHub's diff endpoint does not honour a repository's `.gitattributes`, so
marking such a bundle `-diff` locally shrinks `git diff` but not the PR patch.

Prompts reach the vendor CLI as one argv string, and `execve` caps the whole
argument vector at ARG_MAX (1 MiB on macOS). An oversized patch therefore kills
the job with "[Errno 7] Argument list too long" before the reviewer reads a
single line.

Elide the bodies of oversized file sections rather than truncating the patch
tail: a reviewer needs every filename and every hand-written hunk, and needs
none of the minified bundle. Every elision is announced in-band so the reviewer
can tell a partial patch from a complete one.
"""

MARKER = "[agent-runner]"

# A hand-written file section is rarely over a few KiB; a minified bundle is
# hundreds. The gap is wide enough that one threshold separates them cleanly.
MAX_FILE_BYTES = 32 * 1024

# Backstop for a patch that is large in aggregate rather than in any one file.
# Leaves the rest of the prompt (phase body, plan context, checks) room under
# ARG_MAX.
MAX_TOTAL_BYTES = 384 * 1024

_FILE_HEADER = "diff --git "
_HUNK_HEADER = "@@"


def elide_diff(
    diff_text: str,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> str:
    """Return `diff_text` with oversized file sections replaced by a marker.

    Preserves every file header, so the set of touched paths stays complete
    even when a body is dropped.
    """
    if not diff_text.strip():
        return diff_text

    preamble, sections = _split_sections(diff_text)
    kept = [_elide_section(section, max_file_bytes) for section in sections]
    return _apply_total_budget(preamble, kept, max_total_bytes)


def _split_sections(diff_text: str) -> tuple[list[str], list[list[str]]]:
    """Split a patch into a preamble and one list of lines per file section.

    Only a column-0 `diff --git ` starts a section. Patch body lines are always
    prefixed with a space, `+`, or `-`, so a diff of a diff cannot false-match.
    """
    preamble: list[str] = []
    sections: list[list[str]] = []
    for line in diff_text.splitlines():
        if line.startswith(_FILE_HEADER):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def _elide_section(section: list[str], max_file_bytes: int) -> list[str]:
    if _byte_len(section) <= max_file_bytes:
        return section

    # Everything before the first hunk is metadata a reviewer needs: the paths,
    # the mode and rename lines, the index line. A section with no hunk at all
    # (a pure rename, a "Binary files differ") has nothing safe to drop.
    body_start = _first_hunk_index(section)
    if body_start is None:
        return section

    header = section[:body_start]
    body = section[body_start:]
    return header + [
        f"{_HUNK_HEADER} elided {_HUNK_HEADER}",
        f"{MARKER} elided {len(body)} diff lines ({_human_bytes(_byte_len(body))}) "
        f"from this file section: over the {_human_bytes(max_file_bytes)} per-file "
        f"prompt budget. Read the full patch with `gh pr diff` if it matters.",
    ]


def _apply_total_budget(
    preamble: list[str], sections: list[list[str]], max_total_bytes: int
) -> str:
    total = _byte_len(preamble) + sum(_byte_len(section) for section in sections)
    if total <= max_total_bytes:
        return _join(preamble + [line for section in sections for line in section])

    kept: list[list[str]] = []
    used = _byte_len(preamble)
    for section in sections:
        size = _byte_len(section)
        if kept and used + size > max_total_bytes:
            break
        kept.append(section)
        used += size

    dropped = len(sections) - len(kept)
    lines = preamble + [line for section in kept for line in section]
    if dropped:
        lines.append(
            f"{MARKER} dropped the last {dropped} of {len(sections)} file sections: "
            f"the patch is over the {_human_bytes(max_total_bytes)} total prompt "
            f"budget. Read the full patch with `gh pr diff`."
        )
    return _join(lines)


def _first_hunk_index(section: list[str]) -> int | None:
    for index, line in enumerate(section):
        if line.startswith(_HUNK_HEADER):
            return index
    return None


def _byte_len(lines: list[str]) -> int:
    # +1 per line for the newline `_join` puts back.
    return sum(len(line.encode("utf-8")) + 1 for line in lines)


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def _human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    kib = count / 1024
    if kib < 1024:
        return f"{kib:.1f} KiB"
    return f"{kib / 1024:.1f} MiB"
