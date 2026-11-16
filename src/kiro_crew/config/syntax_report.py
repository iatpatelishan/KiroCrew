"""Name a JSON syntax error in ``config.json`` / ``config.local.json`` for a person.

The loader treats a file that does not parse as absent: it logs one WARNING per
load, marks the file degraded and runs on defaults, which leaves the file itself
untouched. That is the right recovery, but nothing told the person who saved the
broken file: the terminal and the dashboard both read "all systems running" while
every setting they had made was ignored. :func:`find_config_syntax_errors` reads
the two hand-edited files as the person sees them on disk and returns one
:class:`ConfigSyntaxError` per file that does not parse, carrying the path, the
line and column, and one concrete fix. The gateway prints it at start and posts it
as a bell note (``notifications/config_syntax_note.py``).

Read-only: the broken copy is never repaired or moved here. The offending line is
quoted with credential values masked, since the report reaches the log and the bell.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.loader_locations import config_local_path, config_path, read_config_text
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

#: The longest excerpt of the offending line quoted back, so a minified one-line
#: file does not paste its whole body into the terminal and the bell.
_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class ConfigSyntaxError:
    """One config file that does not parse as JSON, and where."""

    path: Path
    line: int
    column: int
    message: str
    excerpt: str
    fix: str
    #: ``<path>.bak`` when one exists: the copy the last settings migration took,
    #: which can be older than the edit that broke the file.
    backup: Path | None = None

    def summary(self) -> str:
        """One line: the file, the position and the parser's reason."""
        return f"{self.path} has a JSON syntax error on line {self.line}, column {self.column}: {self.message}."

    def explain(self) -> str:
        """The summary, the offending line and the fix, for a terminal or a note body."""
        lines = [self.summary()]
        if self.excerpt:
            lines.append(f"  {self.line} | {self.excerpt}")
        # Every config writer refuses a file that does not parse (``save`` and
        # ``read_config_for_update`` raise ConfigReadError), so this holds until it is fixed.
        lines.append(
            "Kiro Crew is ignoring this file and running on default settings; it will not "
            "save over the file until it parses again."
        )
        lines.append(f"To fix it: {self.fix}, save the file, then restart Kiro Crew.")
        if self.backup is not None:
            lines.append(
                f"An earlier copy from a settings migration is at {self.backup}; "
                "it may be older than your last edit."
            )
        return "\n".join(lines)


def _redacted(line: str) -> str:
    """*line* with credential values masked, as the bell's own store masks them."""
    line, _ = redact_exfiltration_urls(line)
    line, _ = redact_credentials(line)
    return line


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _previous_token(text: str, pos: int) -> int:
    """The index of the last non-whitespace character before *pos*, or -1."""
    i = min(pos, len(text)) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return i


def _fix_for(text: str, err: json.JSONDecodeError) -> str:
    """One concrete edit for the commonest hand-edit mistakes, else a generic one."""
    prev = _previous_token(text, err.pos)
    if err.msg.startswith("Expecting ',' delimiter") and prev >= 0:
        return f"add a comma at the end of line {_line_of(text, prev)}"
    if err.msg.startswith("Expecting property name") and prev >= 0 and text[prev] == ",":
        return f"remove the comma at the end of line {_line_of(text, prev)}"
    if err.msg.startswith("Expecting value") and prev >= 0 and text[prev] == ",":
        return f"remove the comma at the end of line {_line_of(text, prev)}"
    return f"correct the JSON on line {err.lineno} at column {err.colno}"


def syntax_error_in(path: Path) -> ConfigSyntaxError | None:
    """The syntax error in the config file at *path*, or None when it parses or is absent.

    Read as the person sees it (:func:`read_config_text`, BOM dropped): the report
    is about the bytes they edited. An unreadable file is the loader's warning to
    give, not a syntax error, so it reads as None here.
    """
    try:
        text = read_config_text(path)
    except (OSError, UnicodeDecodeError):
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError as err:
        lines = text.splitlines()
        excerpt = lines[err.lineno - 1].strip() if 0 < err.lineno <= len(lines) else ""
        # Masked before the cut, so a credential the cut would split is still recognised.
        excerpt = _redacted(excerpt)
        if len(excerpt) > _EXCERPT_CHARS:
            excerpt = excerpt[:_EXCERPT_CHARS] + "..."
        backup = Path(str(path) + ".bak")
        return ConfigSyntaxError(
            path=path,
            line=err.lineno,
            column=err.colno,
            message=err.msg,
            excerpt=excerpt,
            fix=_fix_for(text, err),
            backup=backup if backup.is_file() else None,
        )
    except RecursionError:
        return None
    return None


def find_config_syntax_errors() -> list[ConfigSyntaxError]:
    """The syntax errors in ``config.json`` then ``config.local.json``. BLOCKING: reads files."""
    found = [syntax_error_in(config_path()), syntax_error_in(config_local_path())]
    return [err for err in found if err is not None]
