from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from fabric_domain import (
    CommunicationMode,
    InMemoryRevisionRepository,
    InvalidRevisionTransition,
    Job,
    RevisionLifecycleService,
    RevisionStatus,
    SequentialIdFactory,
    StaleRevision,
    load_revision_file,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


class S6RevisionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_revision_file(FIXTURE)
        self.service = RevisionLifecycleService(
            InMemoryRevisionRepository(),
            SequentialIdFactory(),
        )
        self.project = self.service.create_project("S6 test project")

    def test_draft_validate_commit_lifecycle(self) -> None:
        revision = self.service.create_draft(self.project.id, self.base)

        self.assertEqual(revision.status, RevisionStatus.DRAFT)
        self.assertIsNone(revision.parent_revision_id)
        self.assertEqual(revision.content.revision_id, revision.id)
        self.assertEqual(len(revision.content_hash), 64)

        validation = self.service.validate(revision.id)
        self.assertTrue(validation.report.valid)
        self.assertEqual(validation.revision.status, RevisionStatus.VALIDATED)
        self.assertEqual(validation.revision.content_hash, revision.content_hash)

        committed = self.service.commit(revision.id)
        self.assertEqual(committed.status, RevisionStatus.COMMITTED)
        self.assertEqual(
            self.service.get_project(self.project.id).current_revision_id,
            revision.id,
        )

    def test_unvalidated_revision_cannot_be_committed(self) -> None:
        revision = self.service.create_draft(self.project.id, self.base)

        with self.assertRaises(InvalidRevisionTransition):
            self.service.commit(revision.id)

    def test_invalid_revision_remains_uncommittable(self) -> None:
        invalid_content = replace(
            self.base,
            jobs=(
                replace(self.base.jobs[0], requested_gpus=9),
                self.base.jobs[1],
            ),
        )
        revision = self.service.create_draft(self.project.id, invalid_content)
        validation = self.service.validate(revision.id)

        self.assertFalse(validation.report.valid)
        self.assertEqual(
            validation.revision.status,
            RevisionStatus.VALIDATION_FAILED,
        )
        with self.assertRaises(InvalidRevisionTransition):
            self.service.commit(revision.id)

    def test_second_draft_tracks_committed_parent_and_diff(self) -> None:
        first = self.service.create_draft(self.project.id, self.base)
        self.service.validate(first.id)
        first = self.service.commit(first.id)
        changed_content = replace(
            self.base,
            jobs=(
                replace(self.base.jobs[0], priority=20),
                self.base.jobs[1],
            ),
        )

        second = self.service.create_draft(self.project.id, changed_content)
        diff = self.service.compare(first.id, second.id)

        self.assertEqual(second.parent_revision_id, first.id)
        self.assertNotEqual(first.content_hash, second.content_hash)
        self.assertEqual(diff.changed_count, 1)
        self.assertEqual(diff.entries[0].path, "jobs.job-ring-01")

    def test_compare_reports_added_and_removed_jobs(self) -> None:
        first = self.service.create_draft(self.project.id, self.base)
        extra_job = Job(
            id="job-extra",
            requested_gpus=1,
            communication_mode=CommunicationMode.ALL_TO_ALL,
            priority=0,
        )
        changed_content = replace(
            self.base,
            jobs=(self.base.jobs[0], extra_job),
        )
        second = self.service.create_draft(self.project.id, changed_content)

        diff = self.service.compare(first.id, second.id)

        self.assertEqual(diff.added_count, 1)
        self.assertEqual(diff.removed_count, 1)

    def test_stale_revision_cannot_commit_after_newer_revision(self) -> None:
        first = self.service.create_draft(self.project.id, self.base)
        second = self.service.create_draft(self.project.id, self.base)
        self.service.validate(first.id)
        self.service.validate(second.id)
        self.service.commit(first.id)

        with self.assertRaises(StaleRevision):
            self.service.commit(second.id)

    def test_content_hash_ignores_engine_revision_id(self) -> None:
        first = self.service.create_draft(self.project.id, self.base)
        second = self.service.create_draft(self.project.id, self.base)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_status_change_does_not_mutate_previous_revision(self) -> None:
        revision = self.service.create_draft(self.project.id, self.base)
        original_hash = revision.content_hash
        validated = self.service.validate(revision.id).revision

        self.assertEqual(revision.status, RevisionStatus.DRAFT)
        self.assertEqual(validated.status, RevisionStatus.VALIDATED)
        self.assertEqual(revision.content_hash, original_hash)
        self.assertEqual(validated.content_hash, original_hash)

    def test_inspection_command_shows_lifecycle_and_diff(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s6.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("FabricSandbox S6 revision lifecycle", completed.stdout)
        self.assertIn("jobs.job-ring-01", completed.stdout)
        self.assertIn("Invalid revision commit rejected: YES", completed.stdout)


if __name__ == "__main__":
    unittest.main()
