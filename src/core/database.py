"""SQLite database for storing test run history."""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import config


class Database:
    """Manages test run persistence in SQLite."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.DB_PATH
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT UNIQUE NOT NULL,
                test_name TEXT NOT NULL,
                test_definition TEXT,
                status TEXT DEFAULT 'pending',
                result TEXT,
                duration REAL,
                report_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS test_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                step_number INTEGER,
                action TEXT,
                status TEXT,
                screenshot_path TEXT,
                timestamp TEXT,
                FOREIGN KEY (run_id) REFERENCES test_runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_runs_status ON test_runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_created ON test_runs(created_at);
            CREATE INDEX IF NOT EXISTS idx_steps_run ON test_steps(run_id);
        """)
        conn.commit()
        conn.close()

    def create_run(self, run_id: str, test_name: str, test_definition: str = None) -> dict:
        """Create a new test run record."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO test_runs (run_id, test_name, test_definition, status) VALUES (?, ?, ?, ?)",
            (run_id, test_name, test_definition, "running"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM test_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        return dict(row)

    def update_run(
        self,
        run_id: str,
        status: str = None,
        result: str = None,
        duration: float = None,
        report_path: str = None,
    ):
        """Update a test run record."""
        conn = self._get_conn()
        updates = ["updated_at = datetime('now')"]
        params = []

        if status:
            updates.append("status = ?")
            params.append(status)
        if result:
            updates.append("result = ?")
            params.append(result)
        if duration is not None:
            updates.append("duration = ?")
            params.append(duration)
        if report_path:
            updates.append("report_path = ?")
            params.append(report_path)

        params.append(run_id)
        conn.execute(
            f"UPDATE test_runs SET {', '.join(updates)} WHERE run_id = ?",
            params,
        )
        conn.commit()
        conn.close()

    def get_run(self, run_id: str) -> Optional[dict]:
        """Get a single test run by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM test_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_runs(self, limit: int = 50, status: str = None) -> list[dict]:
        """Get recent test runs."""
        conn = self._get_conn()
        query = "SELECT * FROM test_runs"
        params = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def add_step(self, run_id: str, step_number: int, action: str,
                 status: str = "completed", screenshot_path: str = None):
        """Add a step record for a test run."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO test_steps (run_id, step_number, action, status, screenshot_path, timestamp) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (run_id, step_number, action, status, screenshot_path),
        )
        conn.commit()
        conn.close()

    def get_steps(self, run_id: str) -> list[dict]:
        """Get all steps for a test run."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM test_steps WHERE run_id = ? ORDER BY step_number",
            (run_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get aggregate statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM test_runs").fetchone()["c"]
        passed = conn.execute("SELECT COUNT(*) as c FROM test_runs WHERE status = 'passed'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) as c FROM test_runs WHERE status = 'failed'").fetchone()["c"]
        errors = conn.execute("SELECT COUNT(*) as c FROM test_runs WHERE status = 'error'").fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) as c FROM test_runs WHERE status = 'running'").fetchone()["c"]
        avg_duration = conn.execute("SELECT AVG(duration) as d FROM test_runs WHERE duration IS NOT NULL").fetchone()["d"]
        conn.close()
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "running": running,
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
            "avg_duration": round(avg_duration or 0, 1),
        }

    def delete_run(self, run_id: str):
        """Delete a test run and its steps."""
        conn = self._get_conn()
        conn.execute("DELETE FROM test_steps WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM test_runs WHERE run_id = ?", (run_id,))
        conn.commit()
        conn.close()
