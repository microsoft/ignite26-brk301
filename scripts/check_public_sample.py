"""Check the tracked sample for accidentally included operator artifacts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

PRIVATE_DIRECTORIES = {
    ".azure", ".state", ".foundry", "node_modules", ".venv", "__pycache__",
    "browser-profile", "browser-profiles",
}
PRIVATE_FILES = {
    "environment.local.json", "a365.config.json", "a365.generated.config.json",
    "deployment-state.json", "acceptance.json",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"(?i)(?:[?&])sig=[A-Za-z0-9%/+]{20,}"),
)
TENANT_PATTERN = re.compile(r"(?i)\bcam365\d+\.onmicrosoft\.com\b")
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def problems_for_file(relative: Path, content: str | None) -> list[str]:
    problems = []
    name = relative.name
    if any(part in PRIVATE_DIRECTORIES for part in relative.parts):
        problems.append("private/generated directory")
    if name in PRIVATE_FILES or (
        (name == ".env" or name.startswith(".env."))
        and not name.endswith((".example", ".template"))
    ):
        problems.append("local environment or deployment state")
    if relative.suffix.lower() in {".pem", ".pfx", ".key", ".zip", ".pptx"}:
        problems.append("credential or unreviewed packaged artifact")
    if content is not None:
        if TENANT_PATTERN.search(content):
            problems.append("live demo tenant domain")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            problems.append("credential-shaped content")
    return [f"{relative.as_posix()}: {problem}" for problem in problems]


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"],
        check=True, capture_output=True,
    )
    return sorted({Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name})


def check(root: Path) -> list[str]:
    problems = []
    for relative in tracked_files(root):
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = None
        problems.extend(problems_for_file(relative, content))
        if relative.suffix == ".md" and content:
            for match in LINK_PATTERN.finditer(content):
                target = unquote(match[1].split("#", 1)[0])
                if not target or re.match(r"[a-zA-Z]+:", target):
                    continue
                if not (path.parent / target).exists():
                    problems.append(f"{relative.as_posix()}: broken local link: {target}")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    problems = check(root)
    if problems:
        print("\n".join(problems))
        return 1
    print("Publication checks passed: no blocked artifacts or broken Markdown file links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
