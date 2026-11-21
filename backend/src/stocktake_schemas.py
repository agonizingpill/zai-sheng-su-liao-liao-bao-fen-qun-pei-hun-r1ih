from decimal import Decimal
from pydantic import BaseModel,Field,ConfigDict

class Clean(BaseModel):model_config=ConfigDict(str_strip_whitespace=True)
class StocktakeIn(Clean):code:str=Field(min_length=2,max_length=70)
class QuickCheck(Clean):
    measured_kg:Decimal=Field(gt=0,le=100000,max_digits=12,decimal_places=3)
    measured_melt_index:Decimal=Field(gt=0,le=200,max_digits=9,decimal_places=3)
    measured_moisture:Decimal=Field(ge=0,le=100,max_digits=9,decimal_places=3)
    measured_pvc_ppm:Decimal=Field(ge=0,le=10000,max_digits=9,decimal_places=3)
class RetestIn(QuickCheck):
    bale_id:int=Field(gt=0)
class CandidateIn(QuickCheck):
    mark:str=Field(min_length=2,max_length=70)
class ManualPairing(Clean):
    bale_id:int=Field(gt=0)
    reason:str=Field(min_length=8,max_length=600)
class RetagIn(Clean):
    pairing_ids:list[int]=Field(min_length=1,max_length=50)
    idempotency_key:str|None=Field(default=None,max_length=120)
