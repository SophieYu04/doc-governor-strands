"""Validate executable schema syntax and preserve the original required columns."""
import sqlite3
from pathlib import Path

with sqlite3.connect(':memory:') as database:
    database.executescript(Path('schema.sql').read_text())
    columns = {row[1]: row for row in database.execute('PRAGMA table_info(profiles)')}
    assert columns['id'][2] == 'INTEGER' and columns['id'][5] == 1
    assert columns['display_name'][2] == 'TEXT' and columns['display_name'][3] == 1
