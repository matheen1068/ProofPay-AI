from app.sla import calculate

def test_demo_contract():
    r=calculate();assert r['period_minutes']==43200
    assert r['eligible_downtime_minutes']==120
    assert r['uptime_percent']==99.7222
    assert r['refund_cents']==2000
    assert r['credit_rate_percent']==10

def test_overlapping_events_are_not_double_counted():
    r=calculate(events=[{'id':'a','start':'2026-11-01T00:00:00Z','end':'2026-11-01T01:00:00Z'},
        {'id':'b','start':'2026-11-01T00:30:00Z','end':'2026-11-01T01:30:00Z'}],exclusions=[])
    assert r['eligible_downtime_minutes']==90

def test_exclusion_subtracts_intersection_only():
    r=calculate(events=[{'id':'a','start':'2026-11-01T00:00:00Z','end':'2026-11-01T02:00:00Z'}],
                exclusions=[{'id':'m','start':'2026-11-01T00:30:00Z','end':'2026-11-01T01:00:00Z'}])
    assert r['eligible_downtime_minutes']==90

def test_no_outages_no_refund():
    r=calculate(events=[],exclusions=[])
    assert r['refund_cents']==0 and r['uptime_percent']==100

def test_invalid_interval_rejected():
    import pytest
    with pytest.raises(ValueError): calculate(events=[{'id':'bad','start':'2026-11-02T00:00:00Z','end':'2026-11-01T00:00:00Z'}])

def test_credit_is_never_more_than_fee():
    r=calculate(events=[{'id':'all','start':'2026-11-01T00:00:00Z','end':'2026-12-01T00:00:00Z'}],exclusions=[])
    assert r['refund_cents']==10000
