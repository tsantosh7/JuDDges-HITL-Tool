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
