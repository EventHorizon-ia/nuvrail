"""Regression tests for nuvrail#191: a generated password that starts with '-'.

quickstart_smoke.sh makes its throwaway password with secrets.token_urlsafe,
whose alphabet includes '-'. About 1 run in 64 the password starts with '-',
and the separate-argv form hands argparse something that looks like a flag:

    manage_users.py create e --password -abc      -> argparse: "expected one argument"
    manage_users.py create e --password=-abc      -> value is "-abc", user created

These tests drive the real manage_users.py parser (loaded by path, because
scripts/ is not a package) against a throwaway DB in tmp_path, and pin that
every script calling it with a generated secret uses the `=` form.
"""

import asyncio
import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

from api.auth import verify_password

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "manage_users", REPO_ROOT / "scripts" / "manage_users.py"
)
manage_users = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(manage_users)

# Not a secret: a fixed value with the property that broke CI (leading '-').
LEADING_DASH = "-starts-with-a-dash_like-token-urlsafe"

# Scripts that pass a generated secret to manage_users.py on the command line.
CALLERS = [
    REPO_ROOT / "scripts" / "quickstart_smoke.sh",
    REPO_ROOT / "docs" / "assets" / "3am-demo" / "live_run.sh",
]


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "nuvrail.db"
    monkeypatch.setattr(manage_users, "DB_PATH", path)
    return path


def _stored_hash(db_path: Path, email: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hashed_password FROM users WHERE email = ?", (email,)
        ).fetchone()
    return row[0] if row else None


def test_equals_form_accepts_leading_dash_password(db_path, capsys):
    """The form the smoke script now uses: user created with the exact password."""
    manage_users.main(
        ["create", "smoke@example.com", "--name", "Smoke", f"--password={LEADING_DASH}"]
    )
    assert "Created user 'smoke@example.com'." in capsys.readouterr().out
    hashed = _stored_hash(db_path, "smoke@example.com")
    assert hashed is not None
    assert verify_password(LEADING_DASH, hashed)


def test_separate_form_rejects_leading_dash_password(db_path, capsys):
    """Root cause, pinned: the old `--password VALUE` form dies in argparse.

    If this ever starts passing (argparse behaviour change), the `=` form is
    still correct; the guard test below is what protects the scripts.
    """
    with pytest.raises(SystemExit) as exc:
        manage_users.main(["create", "smoke@example.com", "--password", LEADING_DASH])
    assert exc.value.code == 2
    assert "expected one argument" in capsys.readouterr().err
    assert not db_path.exists() or _stored_hash(db_path, "smoke@example.com") is None


def test_leading_dash_user_can_be_listed(db_path, capsys):
    """Step 4 of the smoke script also runs `list`; the created user must show."""
    manage_users.main(["create", "smoke@example.com", f"--password={LEADING_DASH}"])
    capsys.readouterr()
    asyncio.run(manage_users._list())
    assert "smoke@example.com" in capsys.readouterr().out


@pytest.mark.parametrize("script", CALLERS, ids=lambda p: p.name)
def test_callers_pass_password_with_equals_form(script):
    """No caller may pass a password as a separate argv item (nuvrail#191)."""
    text = script.read_text()
    assert "manage_users.py create" in text, f"{script.name} no longer calls manage_users.py"
    assert re.search(r"--password=\"\$", text), f"{script.name}: expected --password=\"$VAR\""
    # Comments may say `--password -abc` to explain the bug; code lines may not.
    code_bad = [
        m
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for m in re.findall(r"--password\s+\S+", line)
    ]
    assert not code_bad, f"{script.name}: separate-argv password form found: {code_bad}"
