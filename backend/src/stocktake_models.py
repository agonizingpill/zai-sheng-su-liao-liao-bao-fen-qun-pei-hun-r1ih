"""Yard stocktake for bales whose paper tags fell off.

A stocktake session calibrates the difference between the ledger and what the
yard actually measures by re-weighing bales that still have tags.  Untagged
bales are then paired one-to-one with ledger bales that have not yet been
counted.  Pairing is only evidence: a separate retag confirmation writes the
auditable identity recovery.
"""
from decimal import Decimal
from sqlalchemy import String,Numeric,ForeignKey,JSON,CheckConstraint,UniqueConstraint,Integer
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base

class StocktakeSession(Base):
    __tablename__='stocktake_sessions'
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(70),unique=True)
    state:Mapped[str]=mapped_column(String(20),default='open')
    calibration:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))
    closed_by:Mapped[str|None]=mapped_column(String(120),nullable=True)
    closed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class StocktakeRetest(Base):
    """A bale that still carries its tag is re-weighed and quick-checked."""
    __tablename__='stocktake_retests'
    __table_args__=(UniqueConstraint('session_id','bale_id'),CheckConstraint('measured_kg > 0 AND measured_melt_index > 0 AND measured_moisture >= 0 AND measured_pvc_ppm >= 0'))
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('stocktake_sessions.id'))
    bale_id:Mapped[int]=mapped_column(ForeignKey('bales.id'))
    measured_kg:Mapped[Decimal]=mapped_column(Numeric(12,3))
    measured_melt_index:Mapped[Decimal]=mapped_column(Numeric(9,3))
    measured_moisture:Mapped[Decimal]=mapped_column(Numeric(9,3))
    measured_pvc_ppm:Mapped[Decimal]=mapped_column(Numeric(9,3))
    available_snapshot:Mapped[Decimal]=mapped_column(Numeric(12,3))
    ledger_snapshot:Mapped[dict]=mapped_column(JSON)
    deviations:Mapped[dict]=mapped_column(JSON)
    actor:Mapped[str]=mapped_column(String(120))
    at:Mapped[str]=mapped_column(String(40))

class StocktakeCandidate(Base):
    """A physical bale found in the yard without a readable tag."""
    __tablename__='stocktake_candidates'
    __table_args__=(UniqueConstraint('session_id','mark'),CheckConstraint('measured_kg > 0 AND measured_melt_index > 0 AND measured_moisture >= 0 AND measured_pvc_ppm >= 0'))
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('stocktake_sessions.id'))
    mark:Mapped[str]=mapped_column(String(70))
    measured_kg:Mapped[Decimal]=mapped_column(Numeric(12,3))
    measured_melt_index:Mapped[Decimal]=mapped_column(Numeric(9,3))
    measured_moisture:Mapped[Decimal]=mapped_column(Numeric(9,3))
    measured_pvc_ppm:Mapped[Decimal]=mapped_column(Numeric(9,3))
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))

class StocktakePairing(Base):
    """Evidence that an untagged bale is (or may be) a particular ledger bale."""
    __tablename__='stocktake_pairings'
    __table_args__=(UniqueConstraint('session_id','candidate_id'),UniqueConstraint('session_id','bale_id'))
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('stocktake_sessions.id'))
    candidate_id:Mapped[int]=mapped_column(ForeignKey('stocktake_candidates.id'))
    bale_id:Mapped[int|None]=mapped_column(ForeignKey('bales.id'),nullable=True)
    state:Mapped[str]=mapped_column(String(20),default='matched')
    distance:Mapped[Decimal|None]=mapped_column(Numeric(12,4),nullable=True)
    bale_available_snapshot:Mapped[Decimal|None]=mapped_column(Numeric(12,3),nullable=True)
    evidence:Mapped[dict]=mapped_column(JSON)
    note:Mapped[str]=mapped_column(String(600),default='')
    created_by:Mapped[str]=mapped_column(String(120))
    created_at:Mapped[str]=mapped_column(String(40))
    confirmed_by:Mapped[str|None]=mapped_column(String(120),nullable=True)
    confirmed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class StocktakeRetagBatch(Base):
    """Idempotency ledger for a batch retag confirmation."""
    __tablename__='stocktake_retag_batches'
    __table_args__=(UniqueConstraint('session_id','key'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('stocktake_sessions.id'))
    key:Mapped[str]=mapped_column(String(120))
    pairing_ids:Mapped[list]=mapped_column(JSON)
    result:Mapped[dict]=mapped_column(JSON)
    actor:Mapped[str]=mapped_column(String(120))
    at:Mapped[str]=mapped_column(String(40))

class StocktakeRetag(Base):
    """Auditable identity recovery for one physical bale."""
    __tablename__='stocktake_retags'
    __table_args__=(UniqueConstraint('session_id','bale_id'),UniqueConstraint('pairing_id'))
    id:Mapped[int]=mapped_column(primary_key=True)
    session_id:Mapped[int]=mapped_column(ForeignKey('stocktake_sessions.id'))
    pairing_id:Mapped[int]=mapped_column(ForeignKey('stocktake_pairings.id'))
    bale_id:Mapped[int]=mapped_column(ForeignKey('bales.id'))
    candidate_id:Mapped[int]=mapped_column(ForeignKey('stocktake_candidates.id'))
    available_snapshot:Mapped[Decimal]=mapped_column(Numeric(12,3))
    evidence:Mapped[dict]=mapped_column(JSON)
    actor:Mapped[str]=mapped_column(String(120))
    at:Mapped[str]=mapped_column(String(40))
