"""Перевірка інваріантів проєкту, які не можна порушити мовчки.

Grep тут не годиться: у коді є коментарі й докстрінги, які самі згадують
CHANNEL_ID і getUpdates саме для того, щоб пояснити заборону. Тому дивимось
на AST — на реальні імена, рядкові літерали та виклики, а не на текст файлу.

Запуск: python -m tests.check_invariants
"""
from __future__ import annotations

import ast
import pathlib
import sys

BOT = pathlib.Path(__file__).resolve().parent.parent / "bot"

# Що заборонено бачити в РОБОЧОМУ коді збирача (докстрінги не рахуються).
FORBIDDEN = {
    "CHANNEL_ID": "збирач не має права знати про канал — публікує тільки воркер",
    "copyMessage": "копіювання в канал живе виключно у worker/src/index.js",
    "getUpdates": "вебхук назавжди вимикає getUpdates, polling не будуємо",
    "sendPoll": "не наш метод",
    "deleteMessage": "чернетки не видаляються — на них тримається статистика",
}


def docstring_nodes(tree: ast.AST) -> set[int]:
    """id() рядкових вузлів, які є докстрінгами — їх ігноруємо."""
    ignored: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ignored.add(id(body[0].value))
    return ignored


def scan(path: pathlib.Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = docstring_nodes(tree)
    found: list[str] = []

    for node in ast.walk(tree):
        # Рядкові літерали в коді: URL методів, ключі os.environ тощо.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            for needle, why in FORBIDDEN.items():
                if needle in node.value:
                    found.append(f"{path.name}:{node.lineno} рядок містить «{needle}» — {why}")
        # Імена та атрибути: змінні, поля, виклики.
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN:
            found.append(f"{path.name}:{node.lineno} ім'я {node.id} — {FORBIDDEN[node.id]}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            found.append(f"{path.name}:{node.lineno} атрибут .{node.attr} — {FORBIDDEN[node.attr]}")

    return found


def main() -> int:
    problems: list[str] = []
    files = sorted(p for p in BOT.glob("*.py"))
    for path in files:
        problems.extend(scan(path))

    print(f"перевірено файлів у bot/: {len(files)}")
    for needle, why in FORBIDDEN.items():
        print(f"  заборонено «{needle}» — {why}")

    if problems:
        print("\nІНВАРІАНТ ПОРУШЕНО:")
        for line in problems:
            print("  ", line)
        print("\nАвтопублікації в цьому проєкті не існує. Якщо зміна свідома —")
        print("спершу онови CLAUDE.md і .claude/rules/telegram-api.md.")
        return 1

    print("\nOK: у bot/ немає ні доступу до каналу, ні polling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
