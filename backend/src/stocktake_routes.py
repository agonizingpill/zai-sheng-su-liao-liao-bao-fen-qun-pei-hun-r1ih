from fastapi import APIRouter,Depends
from sqlalchemy import select
from .database import Session
from .security import current,allow
from .stocktake_models import StocktakeSession,StocktakeCandidate
from .stocktake_schemas import StocktakeIn,RetestIn,CandidateIn,ManualPairing,RetagIn
from . import stocktake_service as svc
router=APIRouter()

@router.get('/stocktakes')
def stocktakes(u=Depends(current)):
    with Session()as s:return [svc.session_view(s,x)for x in s.scalars(select(StocktakeSession).order_by(StocktakeSession.id.desc()))]

@router.post('/stocktakes')
def create(p:StocktakeIn,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.create(s,p.code,u)

@router.post('/stocktakes/{id}/retests')
def retest(id:int,p:RetestIn,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.retest(s,id,p,u)

@router.post('/stocktakes/{id}/candidates')
def candidate(id:int,p:CandidateIn,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.add_candidate(s,id,p,u)

@router.post('/stocktakes/{id}/pair')
def pair(id:int,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.pair(s,id,u)

@router.post('/stocktakes/{id}/candidates/{cid}/pair')
def manual(id:int,cid:int,p:ManualPairing,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.manual_pair(s,id,cid,p,u)

@router.post('/stocktakes/{id}/retag')
def retag(id:int,p:RetagIn,u=Depends(current)):
    allow(u,'admin','blender')
    with Session.begin()as s:return svc.retag(s,id,p,u)

@router.post('/stocktakes/{id}/close')
def close(id:int,u=Depends(current)):
    allow(u,'admin')
    with Session.begin()as s:return svc.close(s,id,u)
