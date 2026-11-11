"""Additive storage: preparations, execution evidence, and document locks."""
from sqlalchemy import Column, Float, Integer, String, Text

from app.models import Base


class QuotationApprovalReview(Base):
    __tablename__ = 'quotation_approval_reviews'
    key = Column(String(64), primary_key=True)
    digest = Column(String(64), nullable=False)
    operation_id = Column(String(64), nullable=False)
    requester_id = Column(Integer, nullable=False)
    created_at = Column(Float, nullable=False)
    verified = Column(Integer, nullable=False, default=0)


class BusinessPreparation(Base):
    __tablename__ = 'business_preparations'
    id = Column(String(80), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    binding_id = Column(String(80), nullable=False)
    chat_id = Column(String(40), nullable=False)
    action = Column(String(80), nullable=False)
    data_json = Column(Text, nullable=False)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)
    operation_id = Column(String(64), nullable=True)


class BusinessEvidence(Base):
    __tablename__ = 'business_execution_evidence'
    key = Column(String(140), primary_key=True)
    preparation_id = Column(String(80), nullable=False)
    steps_json = Column(Text, nullable=False, default='[]')
    updated_at = Column(Float, nullable=False)


class BusinessTargetLock(Base):
    __tablename__ = 'business_target_locks'
    key = Column(String(64), primary_key=True)
    operation_key = Column(String(140), nullable=False, index=True)
    created_at = Column(Float, nullable=False)


class BusinessContinuation(Base):
    __tablename__ = 'business_continuations'
    id = Column(String(80), primary_key=True)
    user_id = Column(Integer, nullable=False)
    binding_id = Column(String(80), nullable=False)
    chat_id = Column(String(40), nullable=False)
    tenant = Column(String(500), nullable=False)
    database = Column(String(250), nullable=False)
    parent_operation_id = Column(String(64), nullable=False)
    data_json = Column(Text, nullable=False)
    expires_at = Column(Float, nullable=False)
    preparation_id = Column(String(80), nullable=True)
