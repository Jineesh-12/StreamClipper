"""AI backends for choosing clip moments.

Supports direct Gemini API and existing coding-agent CLIs.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .common import Problem

ORDER = ("claude", "codex", "gemini", "opencode")

HOST_HINTS = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM", "CODEX_HOME"),
    "gemini": ("GEMINI_CLI",),
    "opencode": ("OPENCODE", "OPENCODE_BIN_PATH"),
}

TIMEOUT = 900
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"


def _load_project_env() -> None:
    """Load project .env without overriding existing environment variables."""
    if os.environ.get("STREAMCLIPS_NO_ENV_FALLBACK") == "1":
        return

    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        return

    try:
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            if "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()

            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]

            if key and key.replace("_", "").isalnum():
                os.environ.setdefault(key, value)
    except OSError as exc:
        raise Problem(f"Could not read project .env file: {exc}") from exc


_load_project_env()


class NeedAgent(Exception):
    """Agent mode: some prompt files still need an answer file."""

    def __init__(self, waiting: list[tuple[Path, Path]]) -> None:
        super().__init__(f"{len(waiting)} part(s) waiting for answers")
        self.waiting = waiting


class SignedOut(Problem):
    """The brain CLI is installed but not logged in."""


def _get_gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def choose(wanted: str = "auto") -> str:
    if wanted == "gemini-api":
        if not _get_gemini_key():
            raise Problem("GEMINI_API_KEY is not set. Add your Google API Key in Settings.")
        return wanted

    if wanted and wanted != "auto":
        if wanted != "agent" and not shutil.which(wanted):
            raise Problem(
                f"--brain {wanted}: the '{wanted}' command is not installed or not on PATH."
            )
        return wanted

    if _get_gemini_key():
        return "gemini-api"

    for name, hints in HOST_HINTS.items():
        if any(os.environ.get(h) for h in hints) and shutil.which(name):
            return name

    for name in ORDER:
        if shutil.which(name):
            return name

    return "agent"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    return env


def _call(cmd: list[str], stdin_text: Optional[str] = None, cwd: Optional[str] = None) -> str:
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            env=_child_env(),
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise Problem(f"{cmd[0]} did not answer within {TIMEOUT // 60} min") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        error_text = " | ".join(detail[-4:])[:400]
        low = error_text.lower()

        if any(
            k in low
            for k in (
                "authenticate",
                "login",
                "log in",
                "oauth",
                "unauthorized",
                "api key",
                "credentials",
                "not logged",
            )
        ):
            raise SignedOut(
                f"The {cmd[0]} CLI is not signed in. Sign in once or use --brain agent."
            )

        raise Problem(f"{cmd[0]} failed: {error_text}")

    return proc.stdout


def _ask_gemini_api(prompt: str, model: Optional[str] = None) -> str:
    """Send a prompt to Gemini with retries and backup models."""
    api_key = _get_gemini_key()
    if not api_key:
        raise Problem("GEMINI_API_KEY is not set. Add your Google API Key in Settings.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise Problem("Gemini SDK is not installed. Run the project setup again.") from exc

    client = genai.Client(api_key=api_key)

    primary_model = model or DEFAULT_GEMINI_MODEL
    backup_models = [
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
    ]

    models_to_try = list(dict.fromkeys([primary_model] + backup_models))
    last_error: Optional[Exception] = None

    for current_model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=current_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                )

                if not response.text:
                    raise Problem("Gemini API returned an empty response.")

                return response.text

            except Problem:
                raise

            except Exception as exc:  # noqa: BLE001 - retry transient Gemini API errors across fallback models
                last_error = exc
                error_text = str(exc).lower()

                temporary_error = any(
                    marker in error_text
                    for marker in (
                        "429",
                        "500",
                        "502",
                        "503",
                        "504",
                        "unavailable",
                        "high demand",
                        "resource exhausted",
                        "deadline exceeded",
                        "timeout",
                    )
                )

                if temporary_error and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue

                break

    raise Problem(
        f"Gemini API failed on all available models. "
        f"Last error: {str(last_error)[:350]}"
    ) from last_error


def ask(brain: str, prompt: str, model: Optional[str] = None, scratch: Optional[Path] = None) -> str:
    """Send one prompt and return the raw text reply."""
    scratch_dir = scratch or Path(tempfile.gettempdir())

    if brain == "gemini-api":
        return _ask_gemini_api(prompt, model)

    if brain == "claude":
        return _call(
            ["claude", "-p", *(["--model", model] if model else [])],
            prompt,
        )

    if brain == "codex":
        out = scratch_dir / f"codex_reply_{os.getpid()}_{abs(hash(prompt))}.txt"
        _call(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-last-message",
                str(out),
                *(["-m", model] if model else []),
                "-",
            ],
            prompt,
            cwd=str(scratch_dir),
        )
        try:
            return out.read_text(encoding="utf-8")
        finally:
            out.unlink(missing_ok=True)

    if brain == "gemini":
        return _call(
            [
                "gemini",
                *(["-m", model] if model else []),
                "-p",
                "Follow the instructions in the text above and reply with only the JSON array",
            ],
            prompt,
            cwd=str(scratch_dir),
        )

    if brain == "opencode":
        task = scratch_dir / f"opencode_task_{os.getpid()}_{abs(hash(prompt))}.txt"
        task.write_text(prompt, encoding="utf-8")
        try:
            return _call(
                [
                    "opencode",
                    "run",
                    *(["-m", model] if model else []),
                    f"Read the file {task} and do exactly what it asks. "
                    "Reply with only the JSON array it asks for",
                ],
                cwd=str(scratch_dir),
            )
        finally:
            task.unlink(missing_ok=True)

    raise Problem(f"unknown brain '{brain}'")