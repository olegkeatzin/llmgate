#!/usr/bin/env python3
"""Генерирует .env из .env.example со случайными секретами.

Использование:
    python3 tools/gen_secrets.py            # создать .env
    python3 tools/gen_secrets.py --force    # перезаписать существующий
    python3 tools/gen_secrets.py --check    # проверить, что .env заполнен
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"

PLACEHOLDERS = {
    "__GENERATE__": lambda: secrets.token_urlsafe(24),
    "__GENERATE_SK__": lambda: "sk-" + secrets.token_hex(24),
    "__GENERATE_FERNET__": lambda: base64.urlsafe_b64encode(os.urandom(32)).decode(),
}
REQUIRED = ("POSTGRES_PASSWORD", "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "PORTAL_SESSION_KEY")


def generate_text() -> str:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    generated: set[str] = set()
    for line in lines:
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match and match.group(2).strip() in PLACEHOLDERS:
            name, value = match.group(1), match.group(2).strip()
            out.append(name + "=" + PLACEHOLDERS[value]())
            generated.add(name)
        else:
            out.append(line)
    missing = [name for name in REQUIRED if name not in generated]
    if missing:
        print("ВНИМАНИЕ: нет плейсхолдеров для %s — проверьте .env.example" % missing, file=sys.stderr)
    return "\n".join(out) + "\n"


def write_env(text: str) -> None:
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.chmod(TARGET, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def check() -> int:
    if not TARGET.exists():
        print("Файл .env отсутствует. Выполните: python3 tools/gen_secrets.py", file=sys.stderr)
        return 1
    content = TARGET.read_text(encoding="utf-8")
    problems: list[str] = []
    for name in REQUIRED:
        match = re.search(r"^" + name + r"=(.*)$", content, re.MULTILINE)
        if not match or not match.group(1).strip() or match.group(1).startswith("__"):
            problems.append(name)
    if re.search(r"^LDAP_BIND_PASSWORD=\s*$", content, re.MULTILINE):
        print("ВНИМАНИЕ: LDAP_BIND_PASSWORD пуст — вход в портал работать не будет", file=sys.stderr)
    if problems:
        print("Не заполнены секреты: " + ", ".join(problems), file=sys.stderr)
        return 1
    print("OK: .env заполнен")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Генерация .env со случайными секретами")
    parser.add_argument("--force", action="store_true", help="перезаписать существующий .env")
    parser.add_argument("--check", action="store_true", help="только проверить .env")
    args = parser.parse_args()

    if args.check:
        return check()

    if not EXAMPLE.exists():
        print("Не найден %s" % EXAMPLE, file=sys.stderr)
        return 1
    if TARGET.exists() and not args.force:
        print("Файл .env уже существует. Используйте --force, чтобы перезаписать.", file=sys.stderr)
        return 1

    write_env(generate_text())
    print("Создан .env со случайными секретами (права 600).")
    print("Дальше заполните LDAP_* и PUBLIC_API_BASE_URL, затем: make up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
