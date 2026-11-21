"""Yard stocktake pairing for bales whose tags fell off.

A stocktake session learns the measurement deviation from tagged bales that
were re-weighed and quick-checked ("带签复测").  It then makes a one-to-one
batch assignment between untagged physical bales and ledger bales that have not
been counted yet.  Ambiguous pairs are left in ``review`` for a human.  Retag
confirmation is all-or-nothing: it serialises with production release on the
same bale rows and aborts the whole batch if any paired bale's remaining weight
moved after pairing.  Confirmation is idempotent and never writes physical
weights.
"""
import uuid
from decimal import Decimal,ROUND_HALF_UP
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select
from .models import Bale
from .stocktake_models import StocktakeSession,StocktakeRetest,StocktakeCandidate,StocktakePairing,StocktakeRetagBatch,StocktakeRetag
from .blending import obj,get,audit

SCALE=Decimal('0.001')
FINE=Decimal('0.0001')
# Used only when fewer than two tagged retests exist and a spread cannot be learned.
FALLBACK_STD={'kg':Decimal('8'),'melt_index':Decimal('.8'),'moisture':Decimal('.04'),'pvc_ppm':Decimal('12')}
# Quick-check and platform resolution floor.  A small sample can produce a near
# zero spread; without a floor every later bale would look infinitely far away.
MIN_STD={'kg':Decimal('0.5'),'melt_index':Decimal('.15'),'moisture':Decimal('.01'),'pvc_ppm':Decimal('2')}
FEATURES=('kg','melt_index','moisture','pvc_ppm')
QUALITY=('melt_index','moisture','pvc_ppm')
AUTO_THRESHOLD=Decimal('3.0')
AMBIGUITY_RATIO=Decimal('1.5')
DUMMY_COST=1e9
UNRESOLVED=('matched','review','unpaired')

def now():return datetime.now(timezone.utc).isoformat()
def q(v,step=SCALE):return Decimal(v).quantize(step,rounding=ROUND_HALF_UP)

def measured_of(c):
    return {'kg':Decimal(str(c.measured_kg)),'melt_index':Decimal(str(c.measured_melt_index)),
            'moisture':Decimal(str(c.measured_moisture)),'pvc_ppm':Decimal(str(c.measured_pvc_ppm))}
def ledger_of(b):
    """Weight compares to the remaining quantity; quality compares to ledger."""
    return {'kg':Decimal(str(b.available_kg)),'melt_index':Decimal(str(b.melt_index)),
            'moisture':Decimal(str(b.moisture)),'pvc_ppm':Decimal(str(b.pvc_ppm))}

def deviation(ledger,measured):return {f:q(measured[f]-ledger[f],FINE)for f in FEATURES}

def learn(rows):
    model={}
    for f in FEATURES:
        values=[Decimal(str(x['deviation'][f]))for x in rows]
        n=len(values);mean=sum(values,Decimal(0))/n if n else Decimal(0)
        if n>=2:
            var=sum(((v-mean)**2 for v in values),Decimal(0))/(n-1)
            std=var.sqrt()
        else:std=FALLBACK_STD[f]
        std=max(std,MIN_STD[f])
        model[f]={'mean':float(q(mean,FINE)),'std':float(q(std,FINE)),'n':n,'learned':n>=2}
    model.update(rows=len(rows),threshold=float(AUTO_THRESHOLD),ambiguity_ratio=float(AMBIGUITY_RATIO),
                 method='带签复测：实测-台账偏差的均值与样本标准差；重量对剩余可用量，MI/水分/PVC对台账；四项等权欧氏距离')
    return model

def distance(cal,candidate,bale):
    total=Decimal(0);parts={}
    for f in FEATURES:
        z=(candidate[f]-bale[f]-Decimal(str(cal[f]['mean'])))/Decimal(str(cal[f]['std']))
        parts[f]=q(z,FINE);total+=z*z
    return q(total.sqrt(),FINE),parts

def calibration(s,session):
    rows=[x.deviations for x in s.scalars(select(StocktakeRetest).where(StocktakeRetest.session_id==session.id).order_by(StocktakeRetest.bale_id))]
    return learn([{'deviation':r}for r in rows])

def session_view(s,x):
    retests=list(s.scalars(select(StocktakeRetest).where(StocktakeRetest.session_id==x.id).order_by(StocktakeRetest.bale_id)))
    candidates=list(s.scalars(select(StocktakeCandidate).where(StocktakeCandidate.session_id==x.id).order_by(StocktakeCandidate.id)))
    pairings=list(s.scalars(select(StocktakePairing).where(StocktakePairing.session_id==x.id).order_by(StocktakePairing.id)))
    retags=list(s.scalars(select(StocktakeRetag).where(StocktakeRetag.session_id==x.id).order_by(StocktakeRetag.bale_id)))
    return {**obj(x),'calibration':x.calibration,
            'retests':[{**obj(r),'bale_code':s.get(Bale,r.bale_id).code}for r in retests],
            'candidates':[obj(c)for c in candidates],
            'pairings':[{**obj(p),'bale_code':s.get(Bale,p.bale_id).code if p.bale_id else None,
                         'candidate_mark':s.get(StocktakeCandidate,p.candidate_id).mark}for p in pairings],
            'retags':[{**obj(r),'bale_code':s.get(Bale,r.bale_id).code}for r in retags],
            'counts':{'retests':len(retests),'candidates':len(candidates),'pairings':len(pairings),
                      'matched':sum(1 for p in pairings if p.state=='matched'),
                      'review':sum(1 for p in pairings if p.state=='review'),
                      'unpaired':sum(1 for p in pairings if p.state=='unpaired'),
                      'retags':len(retags)}}

def get_open(s,id):
    x=get(s,StocktakeSession,id,True)
    if x.state!='open':raise HTTPException(409,'盘点已结束，不能再录复测、配对或补签')
    return x

def create(s,code,u):
    if s.scalar(select(StocktakeSession.id).where(StocktakeSession.code==code)):raise HTTPException(409,'盘点单号已存在')
    x=StocktakeSession(code=code,state='open',calibration=None,created_by=u['email'],created_at=now())
    s.add(x);s.flush();audit(s,u,'stocktake_opened',x.code);return session_view(s,x)

def retest(s,sid,p,u):
    x=get_open(s,sid)
    b=get(s,Bale,p.bale_id,True)
    if b.available_kg<=0:raise HTTPException(409,'该料包已无剩余物料，不可能仍在堆场复测')
    if s.scalar(select(StocktakeRetest.id).where(StocktakeRetest.session_id==x.id,StocktakeRetest.bale_id==b.id)):raise HTTPException(409,'该带签料包本次盘点已复测，不可重复')
    # Recovering a tag for a bale currently held by an unresolved untagged pair
    # means that guess was wrong: drop it and let the session re-pair.
    held=s.scalar(select(StocktakePairing).where(StocktakePairing.session_id==x.id,StocktakePairing.bale_id==b.id).with_for_update())
    if held:
        if held.state=='paired':raise HTTPException(409,'该料包已补签，不能改按带签复测点账')
        s.delete(held)
    measured={'kg':p.measured_kg,'melt_index':p.measured_melt_index,'moisture':p.measured_moisture,'pvc_ppm':p.measured_pvc_ppm}
    dev=deviation(ledger_of(b),measured)
    row=StocktakeRetest(session_id=x.id,bale_id=b.id,measured_kg=measured['kg'],measured_melt_index=measured['melt_index'],
        measured_moisture=measured['moisture'],measured_pvc_ppm=measured['pvc_ppm'],available_snapshot=b.available_kg,
        ledger_snapshot={'kg':float(b.kg),'available_kg':float(b.available_kg),**{f:float(getattr(b,f))for f in QUALITY}},
        deviations={f:float(v)for f,v in dev.items()},actor=u['email'],at=now())
    s.add(row);s.flush();x.calibration=calibration(s,x);audit(s,u,'stocktake_retested',f'{x.code} / {b.code}');return session_view(s,x)

def add_candidate(s,sid,p,u):
    x=get_open(s,sid)
    if s.scalar(select(StocktakeCandidate.id).where(StocktakeCandidate.session_id==x.id,StocktakeCandidate.mark==p.mark)):raise HTTPException(409,'该无签标记已登记，请勿重复录入')
    c=StocktakeCandidate(session_id=x.id,mark=p.mark,measured_kg=p.measured_kg,measured_melt_index=p.measured_melt_index,
        measured_moisture=p.measured_moisture,measured_pvc_ppm=p.measured_pvc_ppm,created_by=u['email'],created_at=now())
    s.add(c);s.flush();audit(s,u,'stocktake_candidate',x.code+' / '+p.mark);return session_view(s,x)

def _locked_pairings(s,x):
    return list(s.scalars(select(StocktakePairing).where(StocktakePairing.session_id==x.id).order_by(StocktakePairing.id).with_for_update().execution_options(populate_existing=True)))

def _assignments(candidates,bales,cal):
    """Optimal one-to-one assignment (Hungarian). Returns candidate id -> bale id."""
    if not bales:return {},{}
    n,m=len(candidates),len(bales);cols=max(m,n)
    cost=[[DUMMY_COST]*cols for _ in range(n)];dist={}
    for i,c in enumerate(candidates):
        cm=measured_of(c)
        for j,b in enumerate(bales):
            d,parts=distance(cal,cm,ledger_of(b));dist[(c.id,b.id)]=(d,parts);cost[i][j]=float(d)
    col_of_row=_hungarian(cost)
    return {candidates[i].id:bales[j].id for i,j in col_of_row.items() if j<m},dist

def _hungarian(cost):
    """Rectangular min-cost assignment for rows<=cols; returns row index -> col index."""
    n=len(cost);cols=len(cost[0]) if n else 0
    u=[0.0]*(n+1);v=[0.0]*(cols+1);p=[0]*(cols+1);way=[0]*(cols+1)
    for i in range(1,n+1):
        p[0]=i;j0=0;minv=[float('inf')]*(cols+1);used=[False]*(cols+1)
        while True:
            used[j0]=True;i0=p[j0];delta=float('inf');j1=0
            for j in range(1,cols+1):
                if not used[j]:
                    cur=cost[i0-1][j-1]-u[i0]-v[j]
                    if cur<minv[j]:minv[j]=cur;way[j]=j0
                    if minv[j]<delta:delta=minv[j];j1=j
            for j in range(cols+1):
                if used[j]:u[p[j]]+=delta;v[j]-=delta
                else:minv[j]-=delta
            j0=j1
            if p[j0]==0:break
        while True:
            j1=way[j0];p[j0]=p[j1];j0=j1
            if j0==0:break
    return {p[j]-1:j-1 for j in range(1,cols+1) if p[j]}

def _state_for(d1,others):
    """matched only when the assignment is both close and unambiguous."""
    if d1>AUTO_THRESHOLD:return 'review',{'reason':'最优候选距离超过3σ门限，留待人工核对'}
    if others:
        d2=min(others)
        ambiguous=(d2<d1*AMBIGUITY_RATIO) if d1>0 else (d2==0)
        if ambiguous:return 'review',{'reason':'与另一未点账料包距离接近，难以区分，留待人工核对'}
    return 'matched',{}

def pair(s,sid,u):
    """(Re)run the batch assignment for every untagged bale not yet retagged.

    The session row and pairings are locked before bales, matching the retag
    path.  Confirmed retags are frozen; unresolved results (including manual
    choices) are recalculated so a returned batch can be paired again.
    """
    x=get_open(s,sid)
    if not x.calibration:raise HTTPException(409,'先录入带签料包复测，才能学习重量与快检偏差')
    candidates=list(s.scalars(select(StocktakeCandidate).where(StocktakeCandidate.session_id==x.id).order_by(StocktakeCandidate.id)))
    if not candidates:raise HTTPException(409,'尚未录入无签料包，无法配对')
    pairings=_locked_pairings(s,x)
    retagged_c={p.candidate_id for p in pairings if p.state=='paired'}
    retagged_b={p.bale_id for p in pairings if p.state=='paired' and p.bale_id}
    # Confirmed retags stay frozen.  Unresolved rows are recomputed, except a
    # human's manual pick, which stays as a fixed anchor.
    anchored=[]
    for p in pairings:
        if p.state=='paired':continue
        if p.bale_id is not None and (p.evidence or {}).get('manual'):anchored.append(p)
        else:s.delete(p)
    s.flush()
    counted=set(s.scalars(select(StocktakeRetest.bale_id).where(StocktakeRetest.session_id==x.id)))
    stmt=select(Bale).where(Bale.available_kg>0).order_by(Bale.id).with_for_update().execution_options(populate_existing=True)
    if counted:stmt=stmt.where(Bale.id.not_in(counted))
    if retagged_b:stmt=stmt.where(Bale.id.not_in(retagged_b))
    bales=list(s.scalars(stmt))
    fixed_c={p.candidate_id for p in anchored};fixed_b={p.bale_id for p in anchored}
    free_c=[c for c in candidates if c.id not in retagged_c and c.id not in fixed_c]
    free_b=[b for b in bales if b.id not in fixed_b]
    assigned,dist=_assignments(free_c,free_b,x.calibration)
    for p in anchored:
        d,parts=distance(x.calibration,measured_of(s.get(StocktakeCandidate,p.candidate_id)),ledger_of(s.get(Bale,p.bale_id)))
        p.distance=d;p.bale_available_snapshot=s.get(Bale,p.bale_id).available_kg
        p.evidence={**(p.evidence or {}),'distance':float(d),'parts':{k:float(v)for k,v in parts.items()}}
        p.state='matched'
    balemap={b.id:b for b in free_b}
    for c in free_c:
        bid=assigned.get(c.id)
        if bid is None:
            s.add(StocktakePairing(session_id=x.id,candidate_id=c.id,bale_id=None,state='unpaired',distance=None,
                bale_available_snapshot=None,evidence={'reason':'剩余未点账料包不足，留待人工核对'},note='',
                created_by=u['email'],created_at=now()))
            continue
        b=balemap[bid];d,parts=dist[(c.id,bid)]
        others=[v[0]for (cid,_),v in dist.items() if cid==c.id and _!=bid]
        state,extra=_state_for(d,others)
        alts=sorted([(v[0],bid2)for (cid,bid2),v in dist.items() if cid==c.id and bid2!=bid],key=lambda t:(t[0],t[1]))[:3]
        evidence={'distance':float(d),'parts':{k:float(v)for k,v in parts.items()},'threshold':float(AUTO_THRESHOLD),
                  'ambiguity_ratio':float(AMBIGUITY_RATIO),'bale_code':b.code,'bale_available_kg':float(b.available_kg),
                  'alternatives':[{'bale_code':balemap[bid2].code,'distance':float(dd)}for dd,bid2 in alts],
                  **extra}
        s.add(StocktakePairing(session_id=x.id,candidate_id=c.id,bale_id=b.id,state=state,distance=d,
            bale_available_snapshot=b.available_kg,evidence=evidence,note='',created_by=u['email'],created_at=now()))
    s.flush();audit(s,u,'stocktake_paired',x.code);return session_view(s,x)

def manual_pair(s,sid,cid,p,u):
    """Human decision for a review/unpaired row, with an optional one-to-one swap.

    The reason is mandatory.  If the requested ledger bale is tentatively held
    by another unresolved untagged bale, the two swap places, so every bale is
    still paired at most once.  Confirmed retags are immutable.
    """
    x=get_open(s,sid)
    c=s.get(StocktakeCandidate,cid)
    if not c or c.session_id!=x.id:raise HTTPException(404,'无签料包不属于本次盘点')
    reason=p.reason.strip()
    if len(reason)<8:raise HTTPException(409,'人工配对须填写至少8字依据')
    pairings=_locked_pairings(s,x)
    current=next((q for q in pairings if q.candidate_id==cid),None)
    if not current:raise HTTPException(409,'本次盘点还没有配对结果，先执行整批配对')
    if current.state=='paired':raise HTTPException(409,'已补签的无签料包不可改配')
    old_bale_id=current.bale_id
    # Lock the new target and the candidate's current bale in ascending id order,
    # the same order release uses, so a swap cannot deadlock against releasing.
    lock_ids=sorted({p.bale_id,old_bale_id}-{None})
    locked={b.id:b for b in s.scalars(select(Bale).where(Bale.id.in_(lock_ids)).order_by(Bale.id).with_for_update().execution_options(populate_existing=True))}
    b=locked.get(p.bale_id)
    if not b:raise HTTPException(404,'所选料包不存在')
    if b.available_kg<=0:raise HTTPException(409,'该料包已无剩余物料，不可能仍在堆场')
    if s.scalar(select(StocktakeRetest.id).where(StocktakeRetest.session_id==x.id,StocktakeRetest.bale_id==b.id)):raise HTTPException(409,'该料包带签复测已点账，不能再配给无签包')
    occupant=next((q for q in pairings if q.bale_id==b.id and q.id!=current.id),None)
    if occupant and occupant.state=='paired':raise HTTPException(409,'该未点账料包已补签，不能改配')
    if occupant:
        if old_bale_id!=b.id:
            # Vacate the current slot first: (session, bale_id) is unique, so
            # writing the occupant into it before the current row moves would
            # transiently collide.
            current.bale_id=None;s.flush()
            occupant.bale_id=old_bale_id
            occupant.distance=None;occupant.bale_available_snapshot=None
            occupant.state='review' if old_bale_id is not None else 'unpaired'
            occupant.note=f'与 {c.mark} 人工互换，待核对'
            occupant.evidence={**(occupant.evidence or {}),'swap':{'with':c.mark,'from_bale_id':b.id,'to_bale_id':old_bale_id,'actor':u['email'],'at':now(),'reason':reason}}
            if old_bale_id is not None:
                ob=get(s,Bale,old_bale_id)
                d,parts=distance(x.calibration,measured_of(s.get(StocktakeCandidate,occupant.candidate_id)),ledger_of(ob))
                occupant.distance=d;occupant.bale_available_snapshot=ob.available_kg
                occupant.evidence.update(distance=float(d),parts={k:float(v)for k,v in parts.items()},bale_code=ob.code,bale_available_kg=float(ob.available_kg))
            s.flush()
    d,parts=distance(x.calibration,measured_of(c),ledger_of(b))
    current.bale_id=b.id;current.state='matched';current.distance=d;current.bale_available_snapshot=b.available_kg
    current.note=reason
    current.evidence={**(current.evidence or {}),'manual':{'reason':reason,'actor':u['email'],'at':now(),'distance':float(d),'parts':{k:float(v)for k,v in parts.items()}}}
    audit(s,u,'stocktake_paired_manual',f'{x.code} / {c.mark} / {b.code}')
    s.flush();return session_view(s,x)

def retag(s,sid,p,u):
    """Confirm retagging for the selected pairings, all-or-nothing.

    Locks the same bale rows as release and re-checks each paired bale's
    remaining weight against the snapshot taken at pairing.  Any movement
    aborts the whole batch (nothing is written) and the operator must re-pair.

    A repeated request with the same idempotency key returns the first result
    without creating another retag.  The key is re-checked only after the
    pairing rows are locked, so two concurrent retries still produce exactly
    one batch.
    """
    x=get(s,StocktakeSession,sid,True)
    ids=list(dict.fromkeys(p.pairing_ids))
    pairings=_locked_pairings(s,x)
    key=p.idempotency_key or ''
    if key:
        prior=s.scalar(select(StocktakeRetagBatch).where(StocktakeRetagBatch.session_id==x.id,StocktakeRetagBatch.key==key))
        if prior:
            if sorted(prior.pairing_ids)!=sorted(ids):raise HTTPException(409,'该幂等键已用于另一批补签，不能复用')
            return {'replayed':True,'batch':obj(prior),'session':session_view(s,x)}
    if x.state!='open':raise HTTPException(409,'盘点已结束，不能再录复测、配对或补签')
    selected=[q for q in pairings if q.id in ids]
    if len(selected)!=len(ids):raise HTTPException(404,'部分配对记录不属于本次盘点')
    for q in selected:
        if q.state=='paired':raise HTTPException(409,f'{q.id} 已补签，不能重复确认')
        if q.state!='matched':raise HTTPException(409,f'{q.id} 仍待人工核对或无配对目标，不能补签')
        if q.bale_id is None:raise HTTPException(409,f'{q.id} 未配到台账料包，不能补签')
    bale_ids=sorted({q.bale_id for q in selected})
    locked={b.id:b for b in s.scalars(select(Bale).where(Bale.id.in_(bale_ids)).order_by(Bale.id).with_for_update().execution_options(populate_existing=True))}
    moved=[q for q in selected if Decimal(str(locked[q.bale_id].available_kg))!=Decimal(str(q.bale_available_snapshot))]
    if moved:
        codes='、'.join(locked[q.bale_id].code for q in moved)
        raise HTTPException(409,f'配对后余量已变化（{codes}），整批补签退回；请重新整批配对后再确认')
    at=now()
    for q in selected:
        q.state='paired';q.confirmed_by=u['email'];q.confirmed_at=at
        s.add(StocktakeRetag(session_id=x.id,pairing_id=q.id,bale_id=q.bale_id,candidate_id=q.candidate_id,
            available_snapshot=q.bale_available_snapshot,evidence=q.evidence,actor=u['email'],at=at))
    batch=StocktakeRetagBatch(session_id=x.id,key=key or f'auto:{uuid.uuid4().hex}',pairing_ids=ids,
        result={'retagged':len(selected),'bale_ids':bale_ids,'pairing_ids':ids,'at':at},
        actor=u['email'],at=at)
    s.add(batch);s.flush();audit(s,u,'stocktake_retagged',f'{x.code} / {len(selected)} 包')
    return {'replayed':False,'batch':obj(batch),'session':session_view(s,x)}

def close(s,sid,u):
    x=get_open(s,sid)
    pending=list(s.scalars(select(StocktakePairing).where(StocktakePairing.session_id==x.id,StocktakePairing.state.in_(UNRESOLVED))))
    if pending:raise HTTPException(409,f'仍有{len(pending)}个无签包未补签或待人工核对，不能结束盘点')
    x.state='closed';x.closed_by=u['email'];x.closed_at=now();audit(s,u,'stocktake_closed',x.code);return session_view(s,x)
