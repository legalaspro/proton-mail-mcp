"""Bridge's own CLI on this terminal, through a pseudo-terminal, so setup can read the IMAP
username from Bridge's `info` while a person logs in.

The person types the Proton email, password and two-factor code into Bridge's own prompts, and
opens any human-verification link Bridge prints in a browser; none of that passes through an
agent. Setup only watches what Bridge prints: it types `info` once a login succeeds and `exit`
once it has the username. `info` also prints the Bridge password, so only the username is kept,
and each line is dropped once it has been looked at.
"""

import os
import re
import shutil
from collections.abc import Sequence

from .. import localfs

# Colours, bold text and cursor movements, which Bridge's CLI prints around its words.
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\r")
_ADDED = re.compile(r"Account .+ was added successfully")
_USERNAME = re.compile(r"\s*Username:\s+(\S+@\S+)\s*")
# The longest unfinished line kept between reads; nothing Bridge prints needs more.
_LINE_LIMIT = 4096


class InfoReader:
    """Reads Bridge's output a chunk at a time and says what to type next."""

    def __init__(self) -> None:
        self.username = ""
        self._partial = ""
        self._asked_info = False
        self._exited = False

    def feed(self, data: bytes) -> bytes:
        """What to type into Bridge after this output: `info` after a login, `exit` once the
        username is known, or nothing."""
        text = _ESCAPES.sub("", data.decode("utf-8", "replace"))
        *lines, partial = (self._partial + text).split("\n")
        self._partial = partial[-_LINE_LIMIT:]
        reply = b""
        for line in lines:
            match = None if self.username else _USERNAME.fullmatch(line)
            if match:
                self.username = match.group(1)
            if not self._asked_info and _ADDED.search(line):
                self._asked_info = True
                reply += b"info\n"
        if self.username and not self._exited:
            self._exited = True
            reply += b"exit\n"
        return reply


def available() -> bool:
    """Whether a pseudo-terminal can carry the CLI here: not on Windows."""
    return os.name == "posix"


def login_session(argv: Sequence[str]) -> tuple[int, str]:
    """Run Bridge's CLI on this terminal; its exit status, and the username `info` showed or ""
    when it showed none. OSError when it cannot start."""
    if not shutil.which(argv[0]):
        raise FileNotFoundError(argv[0])
    reader = InfoReader()

    def read(descriptor: int) -> bytes:
        data = os.read(descriptor, 1024)
        reply = reader.feed(data)
        if reply:
            os.write(descriptor, reply)
        return data

    return localfs.terminal_session(list(argv), read), reader.username


INSTRUCTIONS = (
    "In Bridge's CLI type `login`, then your Proton email and password (pasting works), and the "
    "two-factor code if it asks. If Bridge prints a verification link, open it in any browser, "
    "finish the check there, then come back and press Enter. Setup then types `info` and `exit` "
    "for you and reads your IMAP username. Already logged in? Type `info` instead of `login`."
)
