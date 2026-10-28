"""Shareable diagnostics contain aggregates, never a raw database or credential file."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import zipfile

from src.log_redact import redact_text
from src.secrets_inventory import is_sanitize_secret_key


def known_secret_values(home: Path) -> set[str]:
    values = {os.environ.get("LLM_API_KEY", ""), os.environ.get("BILI_COOKIE", "")}
    for name in ("cookies.txt", "llm.env"):
        path = home / "config" / name
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if name == "cookies.txt":
            values.add(content.strip())
            for part in content.replace('\n', ';').split(';'):
                if '=' in part:
                    values.add(part.split('=', 1)[1].strip())
        else:
            for line in content.splitlines():
                if '=' in line and is_sanitize_secret_key(line.split('=', 1)[0]):
                    values.add(line.split('=', 1)[1].strip().strip('"\''))
    return {value for value in values if value}


def scrub_known(text: str, values: set[str]) -> str:
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "***")
    return redact_text(text)


def _contains_secret(value, known: set[str]) -> bool:
    if isinstance(value, dict):
        return any((is_sanitize_secret_key(str(k)) and bool(v)) or _contains_secret(v, known) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_secret(v, known) for v in value)
    if isinstance(value, str):
        if redact_text(value) != value or any(v in value for v in known):
            return True
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return False
        return _contains_secret(parsed, known) if isinstance(parsed, (dict, list)) else False
    return False


def inspect_database(database: Path, *, known: set[str] | None = None) -> dict:
    if not database.is_file():
        return {"exists": False, "raw_database_included": False}
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    report = {"exists": True, "raw_database_included": False, "tables": {}, "sensitive_cells": 0, "sensitive_locations": []}
    try:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quote = '"' + table.replace('"', '""') + '"'
            cursor = conn.execute("SELECT * FROM " + quote)
            columns = [entry[0] for entry in cursor.description]
            count = 0
            found = {}
            for row in cursor:
                count += 1
                for column, value in zip(columns, row):
                    if (is_sanitize_secret_key(column) and bool(value)) or _contains_secret(value, known or set()):
                        found[column] = found.get(column, 0) + 1
            report["tables"][table] = count
            for column, cells in found.items():
                report["sensitive_cells"] += cells
                report["sensitive_locations"].append({"table": table, "column": column, "cells": cells})
        return report
    finally:
        conn.close()


def build_shareable_bundle(home: Path, output: Path) -> dict:
    from src.test_isolation import assert_test_path
    assert_test_path(output)
    values = known_secret_values(home)
    report = {"mode": "diagnostic", "shareable": True, "credentials_included": False,
              "database": inspect_database(home / "data" / "binggo.db", known=values),
              "policy": "仅导出汇总，不包含原始数据库、日志、配置、备份或 Cookie/API Key；敏感检查只报告位置与数量"}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Use exclusive creation to avoid accidentally overwriting a private backup.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        def cleaned(value):
            if isinstance(value, str):
                return scrub_known(value, values)
            if isinstance(value, dict):
                return {scrub_known(str(key), values): cleaned(item) for key, item in value.items()}
            if isinstance(value, list):
                return [cleaned(item) for item in value]
            return value
        archive.writestr("diagnostic-summary.json", json.dumps(cleaned(report), ensure_ascii=False, indent=2))
    return {"zip": str(output), **report}
