import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pywatts.ai import launch
from pywatts.ai.launch import LaunchError, build, split

PROMPT = 'You are "the coach".\nUse {mcp_config} & tools; 100 % für Ü.'
WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows command line")


@dataclass
class Launch:
    mcp_config: Path = Path("C:/d a/mcp.json")
    prompt: str = PROMPT
    env: dict = field(default_factory=dict)


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "my tools" / ("claude.exe" if WINDOWS else "claude")
    path.parent.mkdir()
    path.write_bytes(b"x")
    path.chmod(0o755)
    return path


def test_claude_is_restricted(exe):
    start = Launch()
    assert build(start, "", str(exe)) == [
        str(exe),
        "--mcp-config",
        str(start.mcp_config),
        "--strict-mcp-config",
        "--restricted",
        "--tools",
        "",
        "--allowedTools",
        "mcp__pywatts",
        "--append-system-prompt",
        PROMPT,
    ]


def test_claude_on_path(exe, monkeypatch):
    monkeypatch.setenv("PATH", str(exe.parent))
    assert Path(build(Launch(), "  ", "")[0]) == exe


def test_claude_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    with pytest.raises(LaunchError, match="code.claude.com"):
        build(Launch(), "", "")
    with pytest.raises(LaunchError, match="nowhere"):
        build(Launch(), "", "C:/nowhere/claude.exe")


NPM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n"
    'CALL :find_dp0\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n'
    ') ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@a\\cli.js" %*\r\n'
)


def shim(folder, text, target=""):
    path = folder / "claude.cmd"
    path.write_text(text, encoding="utf-8")
    if target:
        file = folder / target
        file.parent.mkdir(parents=True)
        file.write_text("x")
    return path


@windows_only
def test_npm_script_runs_without_cmd(tmp_path):
    path = shim(tmp_path, NPM, "node_modules/@a/cli.js")
    (tmp_path / "node.exe").write_bytes(b"x")
    argv = build(Launch(), "", str(path))
    assert argv[:2] == [
        str(tmp_path / "node.exe"),
        str(tmp_path / "node_modules" / "@a" / "cli.js"),
    ]
    assert argv[2] == "--mcp-config"
    assert argv[argv.index("--tools") + 1] == ""


@windows_only
def test_npm_program_runs_without_cmd(tmp_path):
    text = '@ECHO off\r\n"%dp0%\\node_modules\\c\\bin\\claude.exe"   %*\r\n'
    path = shim(tmp_path, text, "node_modules/c/bin/claude.exe")
    assert build(Launch(), "", str(path))[0].endswith("claude.exe")


@windows_only
@pytest.mark.parametrize("text", [NPM, "@echo off\r\nclaude-real %*\r\n"])  # target missing, other
def test_other_batch_files_are_refused(tmp_path, text):
    path = shim(tmp_path, text)
    with pytest.raises(LaunchError, match="native version"):
        build(Launch(), "", str(path))
    with pytest.raises(LaunchError, match="batch file"):
        build(Launch(), f'"{path}" --x "{{prompt}}"', "")
    assert build(Launch(), f'"{path}" --x 1', "") == [str(path), "--x", "1"]


def test_custom_command(exe):
    start = Launch()
    argv = build(start, f'"{exe}" --mcp "{{mcp_config}}" --sys={{prompt}} "a b"', "ignored")
    # the braces in the prompt stay
    assert argv == [str(exe), "--mcp", str(start.mcp_config), f"--sys={PROMPT}", "a b"]


def test_bad_custom_commands(exe):
    with pytest.raises(LaunchError, match="not found"):
        build(Launch(), "surely-no-such-program --x", "")
    with pytest.raises(LaunchError, match="quot"):
        build(Launch(), f'"{exe} --x', "")
    with pytest.raises(LaunchError, match="not found"):
        build(Launch(), '"" --x', "")
    with pytest.raises(LaunchError, match="character"):
        build(Launch(prompt="a\0b"), f'"{exe}" {{prompt}}', "")


@windows_only
def test_too_long(exe):
    with pytest.raises(LaunchError, match="too long"):
        build(Launch(prompt="x" * 40_000), "", str(exe))


@windows_only
def test_split_like_windows():
    assert split(r'"C:\a b\x.exe" -p "C:\c d" --q "say \"hi\"" ""') == [
        r"C:\a b\x.exe",
        "-p",
        r"C:\c d",
        "--q",
        'say "hi"',
        "",
    ]
    assert split(subprocess.list2cmdline(["x", "", "a b", 'q"u'])) == ["x", "", "a b", 'q"u']
