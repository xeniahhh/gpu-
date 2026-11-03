from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fabric_domain import (
    InMemoryRevisionRepository,
    RevisionLifecycleService,
    RevisionStatus,
    SequentialIdFactory,
    load_revision_file,
)
from gateway.storage import SqliteRevisionRepository


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


class S8PersistenceTest(unittest.TestCase):
    def test_sqlite_revision_lifecycle_survives_repository_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "fabric.db"
            repository = SqliteRevisionRepository(database)
            service = RevisionLifecycleService(
                repository,
                SequentialIdFactory(),
            )
            project = service.create_project("Persistent project")
            revision = service.create_draft(
                project.id,
                load_revision_file(FIXTURE),
            )
            service.validate(revision.id)
            service.commit(revision.id)

            reopened = SqliteRevisionRepository(database)
            reopened_project = reopened.get_project(project.id)
            reopened_revision = reopened.get_revision(revision.id)

            self.assertEqual(reopened_project.current_revision_id, revision.id)
            self.assertEqual(reopened_revision.status, RevisionStatus.COMMITTED)
            self.assertEqual(reopened_revision.content_hash, revision.content_hash)
            self.assertEqual(
                reopened_revision.content.to_dict(),
                revision.content.to_dict(),
            )

    def test_memory_and_sqlite_repositories_share_lifecycle_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repositories = (
                InMemoryRevisionRepository(),
                SqliteRevisionRepository(Path(temp_dir) / "fabric.db"),
            )

            for repository in repositories:
                service = RevisionLifecycleService(
                    repository,
                    SequentialIdFactory(),
                )
                project = service.create_project("Contract project")
                revision = service.create_draft(
                    project.id,
                    load_revision_file(FIXTURE),
                )
                service.validate(revision.id)
                committed = service.commit(revision.id)

                self.assertEqual(committed.status, RevisionStatus.COMMITTED)
                self.assertEqual(
                    service.get_project(project.id).current_revision_id,
                    revision.id,
                )

    def test_postgres_migration_contains_required_tables(self) -> None:
        migration = (
            ROOT / "db" / "migrations" / "001_init.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS projects", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS revisions", migration)
        self.assertIn("content_json JSONB", migration)


if __name__ == "__main__":
    unittest.main()
