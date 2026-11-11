from sqlalchemy import Column, Float, ForeignKey, Integer, LargeBinary, String, Text

from app.models import Base


class ReportJob(Base):
    __tablename__ = "report_jobs"
    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant = Column(String(255), nullable=False)
    database = Column(String(255), nullable=False)
    binding_id = Column(String(64), nullable=False)
    request_json = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="queued", index=True)
    result_json = Column(Text)
    error = Column(Text)
    progress = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(64))
    lease_until = Column(Float)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    delivery_status = Column(String(24), nullable=False, default="pending")
    delivery_index = Column(Integer, nullable=False, default=0)


class ReportArtifact(Base):
    __tablename__ = "report_artifacts"
    id = Column(String(64), primary_key=True)
    report_id = Column(String(64), ForeignKey("report_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    binding_id = Column(String(64), nullable=False)
    filename = Column(String(180), nullable=False)
    content_type = Column(String(120), nullable=False)
    content = Column(LargeBinary, nullable=False)
    expires_at = Column(Float, nullable=False, index=True)
