from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

from app.db import create_app_engine
from app.models import Base, GoogleConnection, User


@pytest.fixture
def drive_db(tmp_path):
    engine = create_app_engine(f"sqlite:///{(tmp_path / 'drive.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        for user_id in (1, 2):
            db.add(
                User(
                    id=user_id,
                    tenant="default",
                    database="odoo",
                    odoo_uid=user_id,
                    login=f"user{user_id}",
                    name="Test user",
                )
            )
        db.flush()
        for user_id in (1, 2):
            db.add(
                GoogleConnection(
                    user_id=user_id,
                    google_sub=f"sub{user_id}",
                    email=f"test{user_id}@example.org",
                    scopes="drive",
                    encrypted_access_token="token",
                    expires_at=datetime.now(UTC),
                )
            )
    yield factory
    engine.dispose()


def test_replacement_encrypts_content_and_rejects_stale_source(drive_db):
    from app.workspace.drive_retriever import DriveRetriever
    from app.workspace.drive_storage import (
        DriveLink,
        DriveStorage,
        IndexedDriveChunk,
        IndexedDriveFile,
    )
    from app.workspace.models import DriveFileChunk, DrivePermissionState

    key = Fernet.generate_key().decode()
    storage = DriveStorage(drive_db, key)
    link = DriveLink(1, "sub1", 1)
    file = IndexedDriveFile("file", "Kế hoạch", "text/plain", "10")
    assert storage.replace_chunks(link, file, [IndexedDriveChunk("Tiến độ dự án", None, "Trang 2")])

    with drive_db() as db:
        row = db.query(DriveFileChunk).one()
        assert row.encrypted_content != "Tiến độ dự án"
        assert (
            Fernet(key.encode()).decrypt(row.encrypted_content.encode()).decode() == "Tiến độ dự án"
        )
        assert "Tiến" not in row.tsv

    retriever = DriveRetriever(drive_db)
    candidates = retriever.search(link, "tien do", None)
    assert [candidate.page_or_section for candidate in candidates] == ["Trang 2"]
    assert retriever.search(link, "Cho tôi hỏi tiến độ như thế nào?") == candidates
    assert (
        retriever.search(link, "dự án còn bao nhiêu việc", terms=["không trùng", "tiến độ"])
        == candidates
    )
    assert storage.decrypt_candidates(link, candidates) == {candidates[0].chunk_id: "Tiến độ dự án"}
    assert storage.decrypt_candidates(link, candidates, require_permission=True) == {}
    with drive_db.begin() as db:
        db.add(
            DrivePermissionState(
                user_id=1,
                google_sub="sub1",
                link_version=1,
                file_id="file",
                scope="my_drive",
                file_version=file.version,
                checked_at=datetime.now(UTC),
            )
        )
    assert storage.decrypt_candidates(link, candidates, require_permission=True) == {
        candidates[0].chunk_id: "Tiến độ dự án",
    }
    with drive_db.begin() as db:
        db.get(DrivePermissionState, (1, "sub1", 1, "file")).checked_at = None
    assert storage.decrypt_candidates(link, candidates, require_permission=True) == {}
    assert storage.is_indexed(link, file)
    assert not storage.is_indexed(link, file, parser_version="new")
    older = IndexedDriveFile("file", "Kế hoạch", "text/plain", "9")
    assert not storage.replace_chunks(link, older, [IndexedDriveChunk("stale", None)])
    assert not retriever.search(DriveLink(2, "sub2", 1), "tiến độ", None)

    with drive_db.begin() as db:
        db.query(GoogleConnection).filter_by(user_id=1).update({"link_version": 2})
    assert not storage.decrypt_candidates(link, candidates)
    assert not storage.replace_chunks(link, file, [IndexedDriveChunk("stale", None)])
    assert not retriever.search(link, "tiến độ", None)


def test_retrieval_fuses_rankings_and_limits_document_count(drive_db):
    from app.workspace.drive_retriever import DriveRetriever
    from app.workspace.drive_storage import (
        DriveLink,
        DriveStorage,
        IndexedDriveChunk,
        IndexedDriveFile,
    )

    link = DriveLink(1, "sub1", 1)
    storage = DriveStorage(drive_db, Fernet.generate_key().decode())
    vector = [1.0, *([0.0] * 1535)]
    for number in range(7):
        file = IndexedDriveFile(str(number), f"Plan {number}", "text/plain", "1")
        assert storage.replace_chunks(
            link,
            file,
            [
                IndexedDriveChunk("tiến độ", vector),
                IndexedDriveChunk("ngày họp", vector),
            ],
            embedding_model="text-embedding-3-small",
        )
    retriever = DriveRetriever(drive_db)
    results = retriever.search(link, "tien do", vector)
    assert len(results) == len({result.chunk_id for result in results}) == 8
    assert len({result.file_id for result in results}) == 4
    assert results[0].score == pytest.approx(2 / 61)
    assert all(not hasattr(result, "encrypted_content") for result in results)
    assert {result.file_id for result in retriever.search(link, "", vector, file_id="6")} == {"6"}


def test_vector_retrieval_discards_unrelated_zero_similarity_chunks(drive_db):
    from app.workspace.drive_retriever import DriveRetriever
    from app.workspace.drive_storage import (
        DriveLink,
        DriveStorage,
        IndexedDriveChunk,
        IndexedDriveFile,
    )

    link = DriveLink(1, "sub1", 1)
    storage = DriveStorage(drive_db, Fernet.generate_key().decode())
    query = [1.0, *([0.0] * 1535)]
    unrelated = [0.0, 1.0, *([0.0] * 1534)]
    assert storage.replace_chunks(
        link,
        IndexedDriveFile("relevant", "Tồn kho", "text/plain", "1"),
        [IndexedDriveChunk("Số lượng tồn kho PN22", query)],
        embedding_model="text-embedding-3-small",
    )
    assert storage.replace_chunks(
        link,
        IndexedDriveFile("unrelated", "Nhật ký", "text/plain", "1"),
        [IndexedDriveChunk("Biên bản đồng bộ", unrelated)],
        embedding_model="text-embedding-3-small",
    )

    results = DriveRetriever(drive_db).search(link, "", query)

    assert [result.file_id for result in results] == ["relevant"]


def test_vi_accent_normalization():
    from app.workspace.vn_normalizer import normalize_vietnamese

    assert normalize_vietnamese("ĐỒNG\u00a0HỒ ＡＢＣ\x00") == "dong ho abc"
