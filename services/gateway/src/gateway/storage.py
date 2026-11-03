from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:
    psycopg = None
    dict_row = None
    Jsonb = None

from fabric_domain import (
    Project,
    ProjectNotFound,
    Revision,
    RevisionContent,
    RevisionNotFound,
    RevisionStatus,
)


def _require_postgres_driver() -> object:
    if psycopg is None or dict_row is None or Jsonb is None:
        raise RuntimeError(
            "PostgreSQL storage requires psycopg[binary]. "
            "Install requirements-demo.txt or select REVISION_STORE=sqlite."
        )
    return psycopg


class SqliteRevisionRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    current_revision_id TEXT
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    parent_revision_id TEXT,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_json TEXT NOT NULL
                );
                """
            )

    def add_project(self, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO projects (id, name, current_revision_id) VALUES (?, ?, ?)",
                (project.id, project.name, project.current_revision_id),
            )

    def get_project(self, project_id: str) -> Project:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name, current_revision_id FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return Project(
            id=row["id"],
            name=row["name"],
            current_revision_id=row["current_revision_id"],
        )

    def save_project(self, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE projects
                SET name = ?, current_revision_id = ?
                WHERE id = ?
                """,
                (project.name, project.current_revision_id, project.id),
            )

    def list_projects(self) -> tuple[Project, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, current_revision_id
                FROM projects
                ORDER BY id
                """
            ).fetchall()
        return tuple(
            Project(
                id=row["id"],
                name=row["name"],
                current_revision_id=row["current_revision_id"],
            )
            for row in rows
        )

    def add_revision(self, revision: Revision) -> None:
        with self._connect() as connection:
            self._insert_revision(connection, revision)

    def get_revision(self, revision_id: str) -> Revision:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, parent_revision_id, status,
                       content_hash, content_json
                FROM revisions
                WHERE id = ?
                """,
                (revision_id,),
            ).fetchone()
        if row is None:
            raise RevisionNotFound(revision_id)
        return self._revision_from_row(row)

    def save_revision(self, revision: Revision) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE revisions SET status = ? WHERE id = ?",
                (revision.status.value, revision.id),
            )

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, parent_revision_id, status,
                       content_hash, content_json
                FROM revisions
                WHERE project_id = ?
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._revision_from_row(row) for row in rows)

    def commit(self, revision: Revision, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE revisions SET status = ? WHERE id = ?",
                (revision.status.value, revision.id),
            )
            connection.execute(
                "UPDATE projects SET current_revision_id = ? WHERE id = ?",
                (project.current_revision_id, project.id),
            )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        revision: Revision,
    ) -> None:
        connection.execute(
            """
            INSERT INTO revisions
                (id, project_id, parent_revision_id, status, content_hash, content_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision.id,
                revision.project_id,
                revision.parent_revision_id,
                revision.status.value,
                revision.content_hash,
                json.dumps(revision.content.to_dict(), ensure_ascii=False),
            ),
        )

    @staticmethod
    def _revision_from_row(row: sqlite3.Row) -> Revision:
        return Revision(
            id=row["id"],
            project_id=row["project_id"],
            parent_revision_id=row["parent_revision_id"],
            status=RevisionStatus(row["status"]),
            content_hash=row["content_hash"],
            content=RevisionContent.from_dict(json.loads(row["content_json"])),
        )


class PostgresRevisionRepository:
    def __init__(self, database_url: str) -> None:
        _require_postgres_driver()
        self.database_url = database_url
        self._initialize()

    def _connect(self) -> psycopg.Connection:
        driver = _require_postgres_driver()
        return driver.connect(self.database_url, row_factory=dict_row)

    def _initialize(self) -> None:
        migration = (
            Path(__file__).resolve().parents[4]
            / "db"
            / "migrations"
            / "001_init.sql"
        )
        if not migration.is_file():
            raise RuntimeError(f"Database migration is missing: {migration}")
        with self._connect() as connection:
            connection.execute(migration.read_text(encoding="utf-8"))

    def add_project(self, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (id, name, current_revision_id)
                VALUES (%s, %s, %s)
                """,
                (project.id, project.name, project.current_revision_id),
            )

    def get_project(self, project_id: str) -> Project:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, current_revision_id
                FROM projects
                WHERE id = %s
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return Project(**row)

    def save_project(self, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE projects
                SET name = %s, current_revision_id = %s
                WHERE id = %s
                """,
                (project.name, project.current_revision_id, project.id),
            )

    def list_projects(self) -> tuple[Project, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, current_revision_id
                FROM projects
                ORDER BY id
                """
            ).fetchall()
        return tuple(Project(**row) for row in rows)

    def add_revision(self, revision: Revision) -> None:
        assert Jsonb is not None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO revisions
                    (id, project_id, parent_revision_id, status, content_hash, content_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    revision.id,
                    revision.project_id,
                    revision.parent_revision_id,
                    revision.status.value,
                    revision.content_hash,
                    Jsonb(revision.content.to_dict()),
                ),
            )

    def get_revision(self, revision_id: str) -> Revision:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, parent_revision_id, status,
                       content_hash, content_json
                FROM revisions
                WHERE id = %s
                """,
                (revision_id,),
            ).fetchone()
        if row is None:
            raise RevisionNotFound(revision_id)
        return self._revision_from_row(row)

    def save_revision(self, revision: Revision) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE revisions SET status = %s WHERE id = %s",
                (revision.status.value, revision.id),
            )

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, parent_revision_id, status,
                       content_hash, content_json
                FROM revisions
                WHERE project_id = %s
                ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        return tuple(self._revision_from_row(row) for row in rows)

    def commit(self, revision: Revision, project: Project) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE revisions SET status = %s WHERE id = %s",
                (revision.status.value, revision.id),
            )
            connection.execute(
                "UPDATE projects SET current_revision_id = %s WHERE id = %s",
                (project.current_revision_id, project.id),
            )

    @staticmethod
    def _revision_from_row(row: dict[str, object]) -> Revision:
        content = row["content_json"]
        if not isinstance(content, dict):
            raise ValueError("PostgreSQL revision content_json must be an object")
        return Revision(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            parent_revision_id=(
                None if row["parent_revision_id"] is None else str(row["parent_revision_id"])
            ),
            status=RevisionStatus(str(row["status"])),
            content_hash=str(row["content_hash"]),
            content=RevisionContent.from_dict(content),
        )
