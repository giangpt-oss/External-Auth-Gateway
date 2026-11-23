import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, cast, delete, func, literal, literal_column, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import GoogleConnection, User
from app.security import decrypt_secret, encrypt_secret
from app.workspace.drive_embedding import validate_embedding
from app.workspace.models import (
    DRIVE_FILE_COLUMNS,
    DriveFileChunk,
    DriveFileMetadata,
    DrivePermissionState,
    DriveVector,
)
from app.workspace.vn_normalizer import normalize_vietnamese

if TYPE_CHECKING:
    from app.workspace.drive_retriever import DriveCandidate


@dataclass(frozen=True, slots=True)
class DriveLink:
    user_id: int
    google_sub: str
    link_version: int


@dataclass(frozen=True, slots=True)
class IndexedDriveFile:
    file_id: str
    name: str
    mime_type: str
    version: str
    modified_time: datetime | None = None
    drive_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndexedDriveChunk:
    content: str = field(repr=False)
    embedding: Sequence[float] | None = field(repr=False)
    page_or_section: str | None = None
    normalized: str | None = field(default=None, repr=False)


def link_predicates(model, link: DriveLink):
    return (
        model.user_id == link.user_id,
        model.google_sub == link.google_sub,
        model.link_version == link.link_version,
    )


def _connected_predicates(link: DriveLink):
    return (
        *link_predicates(GoogleConnection, link),
        GoogleConnection.status == "connected",
        select(User.id).where(User.id == link.user_id, User.active.is_(True)).exists(),
    )


def active_drive_link(link: DriveLink):
    return select(GoogleConnection.id).where(*_connected_predicates(link)).exists()


def lock_drive_link(db: Session, link: DriveLink) -> bool:
    """Serialize publication and permission updates with OAuth relink/disconnect."""
    if db.get_bind().dialect.name == "sqlite":
        result = db.execute(
            update(GoogleConnection)
            .where(*_connected_predicates(link))
            .values(link_version=GoogleConnection.link_version, updated_at=GoogleConnection.updated_at)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1
    return db.scalar(
        select(GoogleConnection.id).where(*_connected_predicates(link)).with_for_update()
    ) is not None


def current_drive_chunks(link: DriveLink):
    """Build a query joined to the source version observed by the sync process."""
    return (
        select()
        .select_from(DriveFileChunk)
        .join(
            DriveFileMetadata,
            and_(
                DriveFileChunk.file_version == DriveFileMetadata.version,
                *(
                    getattr(DriveFileChunk, column) == getattr(DriveFileMetadata, column)
                    for column in DRIVE_FILE_COLUMNS
                ),
            ),
        )
        .where(
            *link_predicates(DriveFileChunk, link),
            DriveFileMetadata.status == "indexed",
            active_drive_link(link),
        )
    )


def vector_literal(embedding: Sequence[float], *, postgres: bool):
    validate_embedding(embedding)
    encoded = json.dumps(list(embedding), separators=(",", ":"))
    return cast(literal(encoded), DriveVector()) if postgres else encoded


class DriveStorage:
    def __init__(self, session_factory: sessionmaker, encryption_key: str):
        self._sessions = session_factory
        self._key = encryption_key

    def is_indexed(
        self,
        link: DriveLink,
        file: IndexedDriveFile,
        *,
        embedding_model: str | None = None,
        parser_version: str = "1",
    ) -> bool:
        with self._sessions() as db:
            return db.scalar(
                select(DriveFileMetadata.file_id).where(
                    *link_predicates(DriveFileMetadata, link),
                    DriveFileMetadata.file_id == file.file_id,
                    DriveFileMetadata.version == file.version,
                    DriveFileMetadata.status == "indexed",
                    DriveFileMetadata.embedding_model == embedding_model,
                    DriveFileMetadata.parser_version == parser_version,
                    active_drive_link(link),
                )
            ) is not None

    def replace_chunks(
        self,
        link: DriveLink,
        file: IndexedDriveFile,
        chunks: Sequence[IndexedDriveChunk],
        *,
        require_metadata: bool = False,
        embedding_model: str | None = None,
        parser_version: str = "1",
    ) -> bool:
        with self._sessions() as db:
            is_postgres = db.get_bind().dialect.name == "postgresql"
            rows = []
            for index, chunk in enumerate(chunks):
                normalized = (
                    chunk.normalized if chunk.normalized is not None else normalize_vietnamese(chunk.content)
                )
                rows.append(DriveFileChunk(
                    user_id=link.user_id,
                    google_sub=link.google_sub,
                    link_version=link.link_version,
                    file_id=file.file_id,
                    file_version=file.version,
                    chunk_index=index,
                    page_or_section=chunk.page_or_section,
                    encrypted_content=encrypt_secret(chunk.content, self._key),
                    tsv=(
                        func.to_tsvector(literal_column("'simple'"), normalized)
                        if is_postgres else " ".join(sorted(set(normalized.split())))
                    ),
                    embedding=(
                        vector_literal(chunk.embedding, postgres=is_postgres)
                        if chunk.embedding is not None else None
                    ),
                ))

            with db.begin():
                if not lock_drive_link(db, link):
                    return False
                key = (link.user_id, link.google_sub, link.link_version, file.file_id)
                metadata = db.get(DriveFileMetadata, key)
                if require_metadata and (
                    metadata is None
                    or metadata.version != file.version
                    or metadata.status not in {"pending", "indexed"}
                ):
                    return False
                if metadata is None:
                    metadata = DriveFileMetadata(
                        user_id=link.user_id, google_sub=link.google_sub,
                        link_version=link.link_version, file_id=file.file_id,
                    )
                    db.add(metadata)
                elif metadata.version != file.version and not (
                    file.version.isascii() and file.version.isdecimal()
                    and metadata.version.isascii() and metadata.version.isdecimal()
                    and int(file.version) > int(metadata.version)
                ):
                    return False

                if not require_metadata:
                    metadata.name = file.name
                    metadata.mime_type = file.mime_type
                    metadata.version = file.version
                    metadata.drive_id = file.drive_id
                    metadata.modified_time = file.modified_time
                metadata.status = "indexed"
                metadata.status_reason = None
                metadata.indexed_at = datetime.now(UTC)
                metadata.embedding_model = embedding_model
                metadata.parser_version = parser_version
                db.flush()
                db.execute(delete(DriveFileChunk).where(
                    *link_predicates(DriveFileChunk, link), DriveFileChunk.file_id == file.file_id,
                ))
                db.add_all(rows)
        return True

    def decrypt_candidates(
        self,
        link: DriveLink,
        candidates: Sequence["DriveCandidate"],
        *,
        require_permission: bool = False,
    ) -> dict[int, str]:
        if not candidates:
            return {}
        expected = {candidate.chunk_id: candidate for candidate in candidates}
        with self._sessions() as db, db.begin():
            if not lock_drive_link(db, link):
                return {}
            query = (
                current_drive_chunks(link)
                .add_columns(
                    DriveFileChunk.id,
                    DriveFileChunk.file_id,
                    DriveFileChunk.file_version,
                    DriveFileChunk.encrypted_content,
                )
                .where(DriveFileChunk.id.in_(expected))
            )
            if require_permission:
                now = datetime.now(UTC)
                query = query.where(
                    select(DrivePermissionState.file_id)
                    .where(
                        *link_predicates(DrivePermissionState, link),
                        DrivePermissionState.file_id == DriveFileChunk.file_id,
                        DrivePermissionState.file_version == DriveFileChunk.file_version,
                        DrivePermissionState.scope == func.coalesce(DriveFileMetadata.drive_id, "my_drive"),
                        DrivePermissionState.checked_at > now - timedelta(seconds=60),
                        DrivePermissionState.checked_at <= now,
                    )
                    .exists()
                )
            decrypted = {}
            for chunk in db.execute(query):
                candidate = expected[chunk.id]
                if candidate.file_id == chunk.file_id and candidate.file_version == chunk.file_version:
                    try:
                        decrypted[chunk.id] = decrypt_secret(chunk.encrypted_content, self._key)
                    except InvalidToken:
                        continue
            return decrypted

    def decrypt_candidate(
        self, link: DriveLink, candidate: "DriveCandidate", *, require_permission: bool = False
    ) -> str | None:
        return self.decrypt_candidates(
            link, [candidate], require_permission=require_permission
        ).get(candidate.chunk_id)
