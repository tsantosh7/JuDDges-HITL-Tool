import time
from sqlalchemy.exc import OperationalError
from sqlalchemy import text

from .db import Base, engine
from . import models  # noqa: F401  (your existing app models)

# ✅ import auth models so tables are registered
from .auth import models as auth_models  # noqa: F401


def _apply_lightweight_migrations():
    """
    This project does not use Alembic yet, so keep additive schema changes here.
    These migrations are intentionally idempotent and do not remove data.
    """
    statements = [
        """
        ALTER TABLE hypothesis_groups
          ADD COLUMN IF NOT EXISTS group_role TEXT NOT NULL DEFAULT 'human_workspace',
          ADD COLUMN IF NOT EXISTS owner_user_id TEXT NULL,
          ADD COLUMN IF NOT EXISTS is_exportable BOOLEAN NOT NULL DEFAULT TRUE,
          ADD COLUMN IF NOT EXISTS sync_locked_by TEXT NULL,
          ADD COLUMN IF NOT EXISTS sync_locked_until TIMESTAMP NULL
        """,
        """
        ALTER TABLE hypothesis_annotations
          ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'human',
          ADD COLUMN IF NOT EXISTS annotation_status TEXT NOT NULL DEFAULT 'synced',
          ADD COLUMN IF NOT EXISTS workspace_user_id TEXT NULL,
          ADD COLUMN IF NOT EXISTS codebook_version TEXT NOT NULL DEFAULT 'v1',
          ADD COLUMN IF NOT EXISTS model_run_id TEXT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hypothesis_annotations_source_doc
          ON hypothesis_annotations(source_type, document_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hypothesis_annotations_workspace_doc
          ON hypothesis_annotations(workspace_user_id, document_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hypothesis_groups_role_enabled
          ON hypothesis_groups(group_role, is_enabled)
        """,
        """
        CREATE TABLE IF NOT EXISTS project_hypothesis_review_groups (
          project_id UUID PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
          group_id TEXT NOT NULL REFERENCES hypothesis_groups(group_id),
          created_by TEXT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_project_hypothesis_review_groups_group
          ON project_hypothesis_review_groups(group_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS project_hypothesis_reviewers (
          project_id UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
          hypothesis_user TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          added_by TEXT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
          PRIMARY KEY (project_id, hypothesis_user)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_project_hypothesis_reviewers_status
          ON project_hypothesis_reviewers(project_id, status)
        """,
        """
        CREATE TABLE IF NOT EXISTS hypothesis_group_reviewers (
          group_id TEXT NOT NULL REFERENCES hypothesis_groups(group_id) ON DELETE CASCADE,
          hypothesis_user TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          requested_by TEXT NULL,
          reviewed_by TEXT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
          PRIMARY KEY (group_id, hypothesis_user)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_hypothesis_group_reviewers_status
          ON hypothesis_group_reviewers(group_id, status)
        """,
        """
        INSERT INTO hypothesis_group_reviewers (
          group_id,
          hypothesis_user,
          status,
          requested_by,
          reviewed_by,
          created_at,
          updated_at
        )
        SELECT
          prg.group_id,
          phr.hypothesis_user,
          phr.status,
          phr.added_by,
          CASE WHEN phr.status IN ('active', 'blocked') THEN phr.added_by ELSE NULL END,
          phr.created_at,
          phr.updated_at
        FROM project_hypothesis_reviewers phr
        JOIN project_hypothesis_review_groups prg
          ON prg.project_id = phr.project_id
        ON CONFLICT (group_id, hypothesis_user) DO NOTHING
        """,
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def init(retries: int = 10, delay: int = 2):
    for attempt in range(retries):
        try:
            Base.metadata.create_all(bind=engine)
            _apply_lightweight_migrations()
            return
        except OperationalError:
            if attempt == retries - 1:
                raise
            print(f"DB not ready, retrying ({attempt + 1}/{retries})...")
            time.sleep(delay)
