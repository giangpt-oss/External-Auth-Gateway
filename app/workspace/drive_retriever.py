import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import func, literal_column
from sqlalchemy.orm import sessionmaker

from app.workspace.drive_embedding import DEFAULT_EMBEDDING_MODEL, validate_embedding
from app.workspace.drive_storage import DriveLink, current_drive_chunks, vector_literal
from app.workspace.models import DriveFileChunk, DriveFileMetadata
from app.workspace.vn_normalizer import normalize_vietnamese

_CANDIDATES_PER_BRANCH = 20
_MAX_CHUNKS = 8
_MAX_FILES = 4
_RRF_CONSTANT = 60
_MIN_VECTOR_SIMILARITY = 0.3
_QUESTION_PREFIXES = (
    "vui long cho toi biet ",
    "cho toi hoi ",
    "cho toi biet ",
    "cho minh hoi ",
    "cho minh biet ",
    "vui long ",
    "hay cho toi biet ",
)
_QUESTION_SUFFIXES = (" nhu the nao", " the nao", " la gi", " o dau", " bao nhieu", " khi nao")


@dataclass(frozen=True, slots=True)
class DriveCandidate:
    chunk_id: int
    file_id: str
    name: str
    drive_id: str | None
    file_version: str
    page_or_section: str | None
    score: float = 0.0


def _fuse_rankings(*rankings: list[DriveCandidate]) -> list[DriveCandidate]:
    candidates = {}
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, candidate in enumerate(ranking, start=1):
            chunk_id = candidate.chunk_id
            candidates[chunk_id] = candidate
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (_RRF_CONSTANT + position)

    file_ids = set()
    result = []
    for chunk_id in sorted(scores, key=lambda key: (-scores[key], key)):
        candidate = candidates[chunk_id]
        if candidate.file_id not in file_ids and len(file_ids) == _MAX_FILES:
            continue
        file_ids.add(candidate.file_id)
        result.append(replace(candidate, score=scores[chunk_id]))
        if len(result) == _MAX_CHUNKS:
            break
    return result


def _search_phrases(query: str, terms: Sequence[str]) -> list[str]:
    phrases = list(dict.fromkeys(filter(None, map(normalize_vietnamese, terms))))
    if phrases:
        return phrases
    normalized = normalize_vietnamese(query)
    for prefix in _QUESTION_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    for suffix in _QUESTION_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized.removesuffix(suffix)
            break
    return [normalized] if normalized else []


class DriveRetriever:
    def __init__(self, session_factory: sessionmaker):
        self._sessions = session_factory

    def search(
        self,
        link: DriveLink,
        query: str,
        query_embedding: Sequence[float] | None = None,
        *,
        file_id: str | None = None,
        embedding_model: str | None = None,
        terms: Sequence[str] = (),
    ) -> list[DriveCandidate]:
        phrases = _search_phrases(query, terms)
        if not phrases and query_embedding is None:
            return []

        base = current_drive_chunks(link).add_columns(
            DriveFileChunk.id,
            DriveFileChunk.file_id,
            DriveFileMetadata.name,
            DriveFileMetadata.drive_id,
            DriveFileChunk.file_version,
            DriveFileChunk.page_or_section,
        )
        if file_id is not None:
            base = base.where(DriveFileChunk.file_id == file_id)
        model = embedding_model or DEFAULT_EMBEDDING_MODEL

        with self._sessions() as db:
            if db.get_bind().dialect.name == "sqlite":
                return _search_sqlite(db, base, phrases, query_embedding, model)

            fts = []
            if phrases:
                simple = literal_column("'simple'")
                tsquery = func.plainto_tsquery(simple, phrases[0])
                for phrase in phrases[1:]:
                    tsquery = tsquery.op("||")(func.plainto_tsquery(simple, phrase))
                matches = (
                    base.where(DriveFileChunk.tsv.bool_op("@@")(tsquery))
                    .order_by(func.ts_rank(DriveFileChunk.tsv, tsquery).desc(), DriveFileChunk.id)
                    .limit(_CANDIDATES_PER_BRANCH)
                )
                fts = [DriveCandidate(*row) for row in db.execute(matches)]

            vector = []
            if query_embedding is not None:
                distance = DriveFileChunk.embedding.op("<=>")(
                    vector_literal(query_embedding, postgres=True)
                )
                matches = (
                    base.where(
                        DriveFileChunk.embedding.is_not(None),
                        DriveFileMetadata.embedding_model == model,
                    )
                    .add_columns(distance.label("vector_distance"))
                    .order_by(distance, DriveFileChunk.id)
                    .limit(_CANDIDATES_PER_BRANCH)
                )
                vector = [
                    DriveCandidate(*row[:-1])
                    for row in db.execute(matches)
                    if 1 - float(row[-1]) >= _MIN_VECTOR_SIMILARITY
                ]
        return _fuse_rankings(fts, vector)


def _search_sqlite(db, base, phrases, query_embedding, embedding_model):
    """Use an in-process equivalent for SQLite development and logic tests."""
    term_sets = [set(phrase.split()) for phrase in phrases]
    fts_matches = []
    if term_sets:
        for *columns, lexemes in db.execute(base.add_columns(DriveFileChunk.tsv)):
            words = set(lexemes.split())
            matched = sum(len(terms) for terms in term_sets if terms <= words)
            if matched:
                fts_matches.append((matched / len(words), DriveCandidate(*columns)))
        fts_matches.sort(key=lambda match: (-match[0], match[1].chunk_id))

    vector_matches = []
    if query_embedding is not None:
        validate_embedding(query_embedding)
        magnitude = math.hypot(*query_embedding)
        matches = base.where(
            DriveFileChunk.embedding.is_not(None),
            DriveFileMetadata.embedding_model == embedding_model,
        )
        for *columns, encoded in db.execute(matches.add_columns(DriveFileChunk.embedding)):
            vector = json.loads(encoded)
            similarity = sum(a * b for a, b in zip(query_embedding, vector, strict=True))
            similarity /= magnitude * math.hypot(*vector)
            if similarity >= _MIN_VECTOR_SIMILARITY:
                vector_matches.append((similarity, DriveCandidate(*columns)))
        vector_matches.sort(key=lambda match: (-match[0], match[1].chunk_id))

    return _fuse_rankings(
        [candidate for _, candidate in fts_matches[:_CANDIDATES_PER_BRANCH]],
        [candidate for _, candidate in vector_matches[:_CANDIDATES_PER_BRANCH]],
    )
