"""Deterministic SLA math: UTC, half-open intervals, exclusion subtraction."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

PERIOD_START = '2026-09-01T00:00:00Z'
PERIOD_END = '2026-10-01T00:00:00Z'
EVENTS = [
    {'id':'INC-101', 'start':'2026-09-07T09:00:00Z','end':'2026-09-07T10:30:00Z', 'detail':'API gateway 5xx incident; synthetic monitoring evidence'},
    {'id':'INC-102', 'start':'2026-09-18T18:00:00Z','end':'2026-09-18T18:30:00Z', 'detail':'Database connection saturation; synthetic monitoring evidence'},
    {'id':'INC-103', 'start':'2026-09-22T02:00:00Z','end':'2026-09-22T02:45:00Z', 'detail':'Planned maintenance, explicitly excluded by SLA'},
]
EXCLUSIONS = [{'id':'MAINT-103','start':'2026-09-22T02:00:00Z','end':'2026-09-22T02:45:00Z'}]

def parse_utc(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('timestamps must include timezone')
    return d.astimezone(timezone.utc)

def merge(ranges):
    out=[]
    for st,en in sorted(ranges):
        if en<=st: continue
        if out and st<=out[-1][1]: out[-1]=(out[-1][0],max(out[-1][1],en))
        else: out.append((st,en))
    return out

def intersect(a,b):
    s=max(a[0],b[0]); e=min(a[1],b[1]);return (s,e) if s<e else None

def duration_minutes(ranges):
    return sum((b-a).total_seconds()/60 for a,b in ranges)

def calculate(events=EVENTS, exclusions=EXCLUSIONS, start=PERIOD_START, end=PERIOD_END, fee_cents=20000):
    period=(parse_utc(start),parse_utc(end))
    if period[0]>=period[1]: raise ValueError('invalid period')
    if not isinstance(fee_cents,int) or fee_cents<0: raise ValueError('invalid fee')
    intervals=[]
    for ev in events:
        r=(parse_utc(ev['start']),parse_utc(ev['end']))
        if r[0]>=r[1]: raise ValueError('invalid incident interval')
        cut=intersect(r,period)
        if cut: intervals.append(cut)
    exc=[]
    for ex in exclusions:
        r=(parse_utc(ex['start']),parse_utc(ex['end']))
        if r[0]>=r[1]: raise ValueError('invalid exclusion interval')
        cut=intersect(r,period)
        if cut: exc.append(cut)
    merged=merge(intervals); exclusions_merged=merge(exc)
    remaining=[]
    for a,b in merged:
        cursor=a
        for x,y in exclusions_merged:
            if y<=cursor or x>=b:continue
            if x>cursor:remaining.append((cursor,min(x,b)))
            cursor=max(cursor,min(y,b))
            if cursor>=b:break
        if cursor<b:remaining.append((cursor,b))
    eligible=duration_minutes(remaining)
    total=(period[1]-period[0]).total_seconds()/60
    uptime=(total-eligible)/total*100
    # Contract policy tiers, not generalized PayPal SLA rules.
    # 99.0 <= uptime <99.9 => 10%; 95<=uptime<99 =>25%; <95 =>50%
    rate=Decimal('0') if uptime>=99.9 else Decimal('.10') if uptime>=99 else Decimal('.25') if uptime>=95 else Decimal('.50')
    refund=int((Decimal(fee_cents)*rate).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    return {'period_start':start,'period_end':end,'period_minutes':int(total), 'eligible_downtime_minutes':eligible,
            'uptime_percent':round(uptime,4),'sla_target_percent':99.9,'credit_rate_percent':int(rate*100),
            'fee_cents':fee_cents,'refund_cents':refund,'event_ids':[e['id'] for e in events],
            'excluded_ids':[e['id'] for e in exclusions], 'evidence_type':'SYNTHETIC_FIXTURE'}
