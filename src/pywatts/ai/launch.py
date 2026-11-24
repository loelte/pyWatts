"""Command line of the AI program. No Qt, no shell."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

INSTALL_URL = "https://code.claude.com/docs"
MAX_COMMAND_CHARS = 32_000  # Windows allows 32767

_SHIMS = (".cmd", ".bat")
# Last line of the batch files npm creates: the program or a script, then all arguments.
_SHIM_CALL = re.compile(r'"%(?:~dp0|dp0%)\\?([^"%]+\.(?:exe|js|cjs|mjs))"\s+%\*', re.IGNORECASE)
_BATCH_SAFE = re.compile(r"[\w\-./\\:=@,+ ]+")


class LaunchError(ValueError):
    """The text is meant for the user."""


class Launch(Protocol):
    mcp_config: Path
    prompt: str
    env: Mapping[str, str]


def build(launch: Launch, command: str, claude_path: str) -> list[str]:
    """command: own command line with {mcp_config} and {prompt}; "" starts Claude Code."""
    if command.strip():
        argv = _custom(launch, command)
    else:
        argv = _claude(launch, claude_path.strip())
    if any("\0" in token for token in argv):
        raise LaunchError("The command contains an invalid character.")
    if sys.platform == "win32" and len(subprocess.list2cmdline(argv)) > MAX_COMMAND_CHARS:
        raise LaunchError("The command line is too long.")
    return argv


def _claude(launch: Launch, claude_path: str) -> list[str]:
    path = claude_path.strip('"')
    found = shutil.which(path or "claude")
    if found is None and path:
        raise LaunchError(f"Claude Code was not found at {path}. Check the path in Settings.")
    if found is None:
        raise LaunchError(
            f"Claude Code was not found. Install it ({INSTALL_URL}), log in once by running "
            "“claude” in a terminal, then restart pyWatts. Or set its path in Settings."
        )
    start = _resolve(found)
    if start is None:
        raise LaunchError(
            "This Claude Code installation starts through a batch file, which cannot take the "
            f"options that restrict it. Install the native version: {INSTALL_URL}"
        )
    # Several options take more than one value: each is ended by the next option.
    return [
        *start,
        "--mcp-config",
        str(launch.mcp_config),
        "--strict-mcp-config",
        "--restricted",
        "--tools",
        "",
        "--allowedTools",
        "mcp__pywatts",
        "--append-system-prompt",
        launch.prompt,
    ]


def _custom(launch: Launch, command: str) -> list[str]:
    values = {"{mcp_config}": str(launch.mcp_config), "{prompt}": launch.prompt}
    try:
        tokens = split(command)
    except ValueError as e:
        raise LaunchError(f"The command cannot be read: {e}") from e
    program, *arguments = [
        # In one pass, so that braces within the prompt stay as they are.
        re.sub(r"\{(?:mcp_config|prompt)\}", lambda m: values[m.group()], token)
        for token in tokens
    ]
    found = shutil.which(program) if program else None
    if found is None:
        raise LaunchError(f"The program “{program}” was not found. Check the command in Settings.")
    start = _resolve(found)
    if start is None:
        if not all(_BATCH_SAFE.fullmatch(a) for a in arguments):
            raise LaunchError(
                f"{found} is a batch file, which cannot take these arguments safely. "
                "Use the program it starts."
            )
        start = [str(Path(found).absolute())]
    return [*start, *arguments]


def split(command: str) -> list[str]:
    """Split like the operating system does. ValueError if the quotes do not match."""
    if sys.platform != "win32":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes

    if len(re.findall(r'(?<!\\)"', command)) % 2:
        raise ValueError("No closing quotation")
    to_argv = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    to_argv.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = to_argv(command.strip(), ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        free(argv)


def _resolve(found: str) -> list[str] | None:
    """The start of the command line. cmd.exe would reinterpret the arguments of a batch file, so
    those of npm are replaced by what they start. None: another batch file."""
    path = Path(found).absolute()
    if sys.platform != "win32" or path.suffix.lower() not in _SHIMS:
        return [str(path)]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:20_000]
    except OSError:
        return None
    calls = set(_SHIM_CALL.findall(text))
    if len(calls) != 1:
        return None
    target = path.parent / calls.pop()
    if not target.is_file():
        return None
    if target.suffix.lower() == ".exe":
        return [str(target)]
    node = path.parent / "node.exe"
    runtime = str(node) if node.is_file() else shutil.which("node.exe")
    return None if runtime is None else [str(Path(runtime).absolute()), str(target)]
