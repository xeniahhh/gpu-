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
    content_json JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS revisions_project_id_idx
    ON revisions(project_id);
