"""Recognize uniform Alpha Curves without changing stored brush data."""
import math
def constant(data):
 from .curves import DEFAULT
 if not isinstance(data,dict):return None
 if data.get('extend') not in {'HORIZONTAL','EXTRAPOLATED'}:return None
 if any(data.get(k)!=v for k,v in DEFAULT.items() if k not in {'points','extend'}):return None
 points=data.get('points',[])
 if len(points)<2 or points[0][0]!=0 or points[-1][0]!=1:return None
 if any(len(p)!=3 or p[2] not in {'VECTOR','AUTO','AUTO_CLAMPED'} for p in points):return None
 if any(not math.isfinite(float(x)) for p in points for x in p[:2]):return None
 if any(a[0]>=b[0] for a,b in zip(points,points[1:])):return None
 value=float(points[0][1])
 if not 0<=value<=1 or any(float(p[1])!=value for p in points):return None
 return value
def fold(brush):
 if not getattr(brush,'alpha_curve_enabled',False):return brush
 value=constant(getattr(brush,'alpha_curve_data',None))
 if value is None:return brush
 from .routing import ResolvedBrush
 return ResolvedBrush(brush,{'alpha':brush.alpha*value,'alpha_curve_enabled':False})
