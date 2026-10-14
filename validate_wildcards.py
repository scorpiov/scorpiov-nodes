#!/usr/bin/env python3
"""
Scorpiov Wildcard File Validator

Scans every .txt file under wildcards/ (recursively, same as the node
itself) and flags lines with problems that would cause the resolver to
silently produce garbled/blended output instead of a clean pick -- most
importantly, unbalanced { } braces from an inline {a|b|c} group that
never got its closing brace.

USAGE:
    python validate_wildcards.py                  # looks for ./wildcards
    python validate_wildcards.py /path/to/wildcards

This is a standalone read-only check -- it never modifies your files.

NOTE: comment-stripping logic here is intentionally kept in sync with
_strip_comments_from_lines() in scorpiov_wildcard.py. If that function's
comment rules ever change, update this copy to match, or a comment
containing a stray { or } could produce a false positive/negative here.
"""

import sys
import os


# ── Comment stripping (kept in sync with scorpiov_wildcard.py) ─────────────
# Rules: '#' starts a to-end-of-line comment; '\#' is an escape producing a
# literal '#'; '/*' ... '*/' are block comments recognised anywhere.
def strip_comments_from_lines(lines):
    out = []
    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        kept = []
        i = 0
        n = len(line)
        while i < n:
            if in_block:
                end_idx = line.find("*/", i)
                if end_idx == -1:
                    i = n
                else:
                    in_block = False
                    i = end_idx + 2
                continue

            ch = line[i]
            if ch == "\\" and i + 1 < n and line[i + 1] == "#":
                kept.append("#")
                i += 2
                continue
            if ch == "#":
                i = n
                continue
            if ch == "/" and i + 1 < n and line[i + 1] == "*":
                in_block = True
                i += 2
                continue
            kept.append(ch)
            i += 1

        cleaned = "".join(kept).strip()
        if cleaned:
            out.append(cleaned)

    return out


# ── Brace-balance check (stack-based, catches wrong-order too) ─────────────
def check_braces(line):
    """
    Returns a list of problem strings for this line, empty if clean.
    Uses a stack so "}...{"  (closed before opened) is caught too, not
    just a simple open/close count mismatch.
    """
    problems = []
    stack = []
    for pos, ch in enumerate(line):
        if ch == "{":
            stack.append(pos)
        elif ch == "}":
            if not stack:
                problems.append(f"'}}' at position {pos} has no matching '{{' before it")
            else:
                stack.pop()

    if stack:
        positions = ", ".join(str(p) for p in stack)
        count = len(stack)
        plural = "s" if count > 1 else ""
        problems.append(f"{count} unclosed '{{' (position{plural}: {positions})")

    return problems


def check_lone_group_no_pipe(line):
    """
    Flags a {something} group with no | inside it at all -- usually a
    sign a second option was meant to be added but never was, or a
    leftover { } from editing that should just be removed.
    """
    problems = []
    depth = 0
    start = None
    for pos, ch in enumerate(line):
        if ch == "{":
            if depth == 0:
                start = pos
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                inner = line[start + 1:pos]
                if "|" not in inner and inner.strip():
                    problems.append(
                        f"'{{...}}' at position {start} has no '|' inside it "
                        f"-- single-option group, probably not intended"
                    )
                start = None
    return problems


LONG_LINE_THRESHOLD = 600  # chars -- worth a manual look, not necessarily wrong


def scan_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()

    cleaned_lines = strip_comments_from_lines(raw_lines)

    issues = []
    for line_num, line in enumerate(cleaned_lines, start=1):
        brace_problems = check_braces(line)
        pipe_problems = check_lone_group_no_pipe(line) if not brace_problems else []

        for problem in brace_problems:
            issues.append(("ERROR", line_num, problem, line))
        for problem in pipe_problems:
            issues.append(("WARN", line_num, problem, line))

        if len(line) > LONG_LINE_THRESHOLD:
            issues.append((
                "INFO", line_num,
                f"line is {len(line)} chars -- worth a manual look in case "
                f"multiple templates got merged together",
                line,
            ))

    return issues


def main():
    args = sys.argv[1:]
    show_info = "--show-info" in args
    positional = [a for a in args if a != "--show-info"]
    wildcards_dir = positional[0] if positional else "wildcards"

    if not os.path.isdir(wildcards_dir):
        print(f"Directory not found: {wildcards_dir}")
        print("Run this from inside scorpiov-nodes/, or pass the path explicitly:")
        print("    python validate_wildcards.py /path/to/wildcards")
        sys.exit(1)

    txt_files = []
    for root, _dirs, files in os.walk(wildcards_dir):
        for fname in sorted(files):
            if fname.lower().endswith(".txt"):
                txt_files.append(os.path.join(root, fname))
    txt_files.sort()

    if not txt_files:
        print(f"No .txt files found under {wildcards_dir}")
        sys.exit(0)

    total_errors = 0
    total_warnings = 0
    total_info = 0
    any_issues = False

    for path in txt_files:
        issues = scan_file(path)
        if not issues:
            continue

        # Count everything (including INFO) for the summary line, but only
        # print ERROR/WARN entries -- INFO (long-line heads-ups) are counted
        # and mentioned in the summary total, just not listed one by one.
        for severity, _line_num, _problem, _line_text in issues:
            if severity == "ERROR":
                total_errors += 1
            elif severity == "WARN":
                total_warnings += 1
            else:
                total_info += 1

        visible_severities = ("ERROR", "WARN", "INFO") if show_info else ("ERROR", "WARN")
        visible_issues = [i for i in issues if i[0] in visible_severities]
        if not visible_issues:
            continue

        any_issues = True
        print(f"\n{path}")
        print("-" * len(path))
        for severity, line_num, problem, line_text in visible_issues:
            preview = line_text.strip()
            if len(preview) > 120:
                preview = preview[:117] + "..."

            print(f"  [{severity}] line {line_num}: {problem}")
            print(f"      {preview}")

    info_note = "" if show_info else f" ({total_info} long-line info note(s) not shown -- pass --show-info to see them)"

    print("\n" + "=" * 60)
    if total_errors or total_warnings:
        print(f"Scanned {len(txt_files)} file(s). "
              f"{total_errors} error(s), {total_warnings} warning(s){info_note}.")
        print("\nERROR = will actually break resolution (unbalanced braces).")
        print("WARN  = probably a mistake, but won't break anything ({ } with no |).")
    elif total_info:
        print(f"Scanned {len(txt_files)} file(s). No errors or warnings.{info_note}")
    else:
        print(f"Scanned {len(txt_files)} file(s). No issues found.")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
