from __future__ import annotations

import json
from pathlib import Path

from fabric_domain.models import RevisionContent


def load_revision_file(path: str | Path) -> RevisionContent:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    return RevisionContent.from_dict(payload)


def revision_from_json(payload: str) -> RevisionContent:
    return RevisionContent.from_dict(json.loads(payload))


def revision_to_json(content: RevisionContent, *, indent: int = 2) -> str:
    return (
        json.dumps(
            content.to_dict(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )
        + "\n"
    )
