# api/app/models.py

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from pydantic import BaseModel, Field

from .db import Base


class Team(Base):
    __tablename__ = "teams"
    team_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, unique=True)


class Project(Base):
    __tablename__ = "projects"
    project_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.team_id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    team = relationship("Team")


class Document(Base):
    __tablename__ = "documents"

    document_id = Column(String, primary_key=True)
    canonical_url = Column(String, unique=True, nullable=False)
    published_date = Column(Date, nullable=True)
    doc_type = Column(String, nullable=True)
    title = Column(String, nullable=True)
    excerpt = Column(Text, nullable=True)
    content_text = Column(Text, nullable=False)
    source = Column(String, nullable=True)
    doc_metadata = Column(JSON, nullable=False, default=dict)

    hypothesis_annotations = relationship(
        "HypothesisAnnotation",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class ProjectDocument(Base):
    __tablename__ = "project_documents"

    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.project_id"), primary_key=True)
    document_id = Column(String, ForeignKey("documents.document_id"), primary_key=True)

    project = relationship("Project")
    document = relationship("Document")


class HypothesisGroup(Base):
    __tablename__ = "hypothesis_groups"

    group_id = Column(String, primary_key=True)
    name = Column(String, nullable=False, default="")
    organization = Column(String, nullable=True)
    scopes = Column(JSON, nullable=False, default=list)
    is_enabled = Column(Boolean, nullable=False, default=True)

    # ✅ incremental sync cursor (Hypothesis `updated` string)
    last_synced_updated = Column(String, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class HypothesisAnnotation(Base):
    __tablename__ = "hypothesis_annotations"

    annotation_id = Column(String, primary_key=True)
    group_id = Column(String, ForeignKey("hypothesis_groups.group_id"), nullable=False)

    document_id = Column(String, ForeignKey("documents.document_id"), nullable=True)
    canonical_url = Column(String, nullable=True)

    user = Column(String, nullable=True)
    created = Column(DateTime, nullable=True)
    updated = Column(DateTime, nullable=True)

    text = Column(Text, nullable=True)
    tags = Column(JSON, nullable=False, default=list)

    exact = Column(Text, nullable=True)
    prefix = Column(Text, nullable=True)
    suffix = Column(Text, nullable=True)

    raw = Column(JSON, nullable=False, default=dict)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    group = relationship("HypothesisGroup")
    document = relationship("Document", back_populates="hypothesis_annotations")

class Code(Base):
    """
    Canonical code registry.
    - version: "v1" (original 43, locked) or "ext" (schema evolution)
    """
    __tablename__ = "codes"

    code = Column(String, primary_key=True)          # canonical tag string, e.g. "Appellant"
    version = Column(String, nullable=False)         # "v1" | "ext"
    display_name = Column(String, nullable=True)
    description = Column(Text, nullable=True)

    is_active = Column(Boolean, nullable=False, default=True)
    is_locked = Column(Boolean, nullable=False, default=False)  # True for v1

    created_by = Column(String, nullable=True)       # optional user id/email
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    aliases = relationship(
        "CodeAlias",
        back_populates="code_ref",
        cascade="all, delete-orphan",
    )


class CodeAlias(Base):
    """
    Alias -> canonical mapping (supports renames, typos, old tags).
    """
    __tablename__ = "code_aliases"

    alias = Column(String, primary_key=True)          # e.g. "Confess Plead"
    code = Column(String, ForeignKey("codes.code"), nullable=False)  # canonical

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    code_ref = relationship("Code", back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("alias", name="uq_code_alias"),
    )





class ProjectDocumentReview(Base):
    __tablename__ = "project_document_reviews"

    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.project_id"), primary_key=True)
    document_id = Column(String, ForeignKey("documents.document_id"), primary_key=True)

    status = Column(String, nullable=False, default="unseen")  # unseen|in_progress|done|disputed
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(String, nullable=True)


# --- Topic Assignment models ---


class TopicRun(Base):
    __tablename__ = "topic_runs"

    run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # # If null => personal (user-only) run; if set => project-scoped run (optional future)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=True)

    name = Column(String, nullable=False)
    topic_schema_version = Column(String, nullable=False, default="topics-v1")

    method = Column(String, nullable=False, default="external")  # llm|lda|bertopic|rules|external
    model = Column(String, nullable=True)
    params = Column(JSON, nullable=False, default=dict)

    # activation: the run whose topics are pushed into Solr for search/filter
    is_active = Column(Boolean, nullable=False, default=False)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DocumentTopic(Base):
    __tablename__ = "document_topics"

    run_id = Column(UUID(as_uuid=True), ForeignKey("topic_runs.run_id"), primary_key=True)
    document_id = Column(String, ForeignKey("documents.document_id"), primary_key=True)

    topic_key = Column(String, primary_key=True)
    topic_label = Column(String, nullable=False)
    score = Column(Float, nullable=True)

    # keep source/evidence if you still want them
    source = Column(String, nullable=False, default="model")
    evidence = Column(JSON, nullable=False, default=dict)

    # ✅ NEW (matches your ALTER TABLE + main.py usage)
    assignment_type = Column(Text, nullable=False, default="auto")     # auto|human
    status = Column(Text, nullable=False, default="active")            # active|deleted|rejected
    created_by = Column(Text, nullable=True)
    updated_by = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())




class DocEmbedding(Base):
    __tablename__ = "doc_embeddings"
    document_id = Column(String, ForeignKey("documents.document_id"), primary_key=True)
    embedding_dim = Column(Integer, nullable=False)
    model = Column(String, nullable=False)
    embedding = Column(LargeBinary, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
