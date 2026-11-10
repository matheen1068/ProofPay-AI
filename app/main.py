"""ProofPay Guarantee — demonstrator, sandbox-only money movements."""
import os, json, sqlite3, secrets, uuid, pathlib, threading
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from .sla import calculate, EVENTS, EXCLUSIONS

BASE=pathlib.Path(__file__).resolve().parent
DB_PATH=os.getenv('DATABASE_PATH',str(BASE.parent/'data'/'proofpay.db'))
SANDBOX='https://api-m.sandbox.paypal.com'
app=FastAPI(title='ProofPay Guarantee',version='0.1.0')
lock=threading.RLock()

def conn():
    pathlib.Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DB_PATH,timeout=15,check_same_thread=False)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
        detail TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT UNIQUE NOT NULL,
        capture_id TEXT UNIQUE, state TEXT NOT NULL, amount_cents INTEGER NOT NULL, approved_at TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS investigations(id INTEGER PRIMARY KEY AUTOINCREMENT,
        result TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY AUTOINCREMENT,
        capture_id TEXT UNIQUE NOT NULL, paypal_refund_id TEXT, state TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, request_id TEXT UNIQUE NOT NULL)''')
    db.commit();return db

def record(db,kind,detail):
    db.execute('INSERT INTO events(kind,detail,created_at) VALUES(?,?,?)',(kind,json.dumps(detail),datetime.now(timezone.utc).isoformat()))

def auth(token):
    expected=os.getenv('ADMIN_TOKEN','')
    if not expected or len(expected)<20:raise HTTPException(503,'ADMIN_TOKEN must be set to 20+ characters')
    if not token or not secrets.compare_digest(token.replace('Bearer ','').strip(),expected):
        raise HTTPException(401,'Invalid operator token')

def configured():
    if not os.getenv('PAYPAL_CLIENT_ID') or not os.getenv('PAYPAL_CLIENT_SECRET'):
        raise HTTPException(503,'PayPal sandbox credentials not configured')
    if os.getenv('PAYPAL_BASE_URL',SANDBOX)!=SANDBOX:
        raise HTTPException(503,'Only PayPal sandbox is permitted')

def public_url():
    url=os.getenv('PUBLIC_BASE_URL','http://localhost:8000').rstrip('/')
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(503,'Invalid PUBLIC_BASE_URL')
    if parsed.scheme!='https' and parsed.hostname not in ('localhost','127.0.0.1'):
        raise HTTPException(503,'Public URL must use HTTPS')
    return url

async def paypal(method,path,payload=None,request_id=None):
    configured()
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token_response=await client.post(SANDBOX+'/v1/oauth2/token',auth=(os.environ['PAYPAL_CLIENT_ID'],os.environ['PAYPAL_CLIENT_SECRET']),data={'grant_type':'client_credentials'})
            token_response.raise_for_status()
            token=token_response.json()['access_token']
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
            if request_id:headers['PayPal-Request-Id']=request_id
            response=await client.request(method,SANDBOX+path,headers=headers,json=payload)
        except (httpx.RequestError,httpx.TimeoutException) as exc:
            # Deliberately DO NOT retry an uncertain financial mutation.
            raise HTTPException(503,'PayPal request outcome uncertain; inspect PayPal activity before retrying') from exc
    if response.status_code>=400:
        try: reason=response.json().get('details',[{}])[0].get('issue',response.json().get('name','PayPal error'))
        except Exception: reason='PayPal request failed'
        raise HTTPException(502,f'PayPal {response.status_code}: {reason}')
    return response.json() if response.content else {}

@app.get('/',response_class=HTMLResponse)
def page():return (BASE/'index.html').read_text()

@app.get('/api/status')
def status():
    with conn() as db:
        payment=db.execute('SELECT * FROM payments ORDER BY id DESC LIMIT 1').fetchone()
        refund=db.execute('SELECT * FROM refunds ORDER BY id DESC LIMIT 1').fetchone()
        investigation=db.execute('SELECT * FROM investigations ORDER BY id DESC LIMIT 1').fetchone()
        timeline=[dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 25')]
        return {'assessment':calculate(),'events':EVENTS,'exclusions':EXCLUSIONS,'payment':dict(payment) if payment else None,
                'refund':dict(refund) if refund else None,'investigation':json.loads(investigation['result']) if investigation else None,
                'timeline':timeline,'paypal_configured':bool(os.getenv('PAYPAL_CLIENT_ID') and os.getenv('PAYPAL_CLIENT_SECRET')),
                'ai_configured':bool(os.getenv('OPENAI_API_KEY'))}

class Inquiry(BaseModel):
    additional_question:str=''

@app.post('/api/investigate')
async def investigate(payload:Inquiry,authorization:str|None=Header(default=None)):
    auth(authorization)
    key=os.getenv('OPENAI_API_KEY','')
    if not key:raise HTTPException(503,'OPENAI_API_KEY not configured; no fake AI output')
    facts=calculate()
    system='You are an evidence-limited SLA analyst. You MUST NOT decide payment amount, authorize refunds, invent observations or claim synthetic evidence is verified. Return valid JSON with keys summary, event_citations (array of incident IDs), limitations, recommended_action. Keep summary under 80 words. The recommendation is advisory only.'
    prompt=json.dumps({'contract':'Synthetic demo: 99.9% uptime, billing month November 2026 UTC, credit tiers 10% for uptime >=99 and <99.9, 25% for >=95 and <99, 50% below 95, all capped by paid fee','monitoring':EVENTS,'maintenance_exclusions':EXCLUSIONS,'deterministic_calculation':facts,'question':payload.additional_question[:500]})
    async with httpx.AsyncClient(timeout=35) as client:
        try:
            r=await client.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={
                'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'temperature':0,'response_format':{'type':'json_object'},
                'messages':[{'role':'system','content':system},{'role':'user','content':prompt}]})
            r.raise_for_status()
            obj=json.loads(r.json()['choices'][0]['message']['content'])
        except Exception as exc:
            raise HTTPException(502,'AI investigation failed; no assessment saved') from exc
    ids={e['id'] for e in EVENTS}
    cites=obj.get('event_citations',[])
    if not isinstance(cites,list) or any(x not in ids for x in cites):raise HTTPException(502,'AI returned invalid evidence citations')
    result={'summary':str(obj.get('summary',''))[:600], 'event_citations':cites,
            'limitations':str(obj.get('limitations',''))[:600], 'recommended_action':str(obj.get('recommended_action',''))[:400],
            'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'evidence_type':'SYNTHETIC_FIXTURE'}
    with lock,conn() as db:
        db.execute('INSERT INTO investigations(result,created_at) VALUES(?,?)',(json.dumps(result),datetime.now(timezone.utc).isoformat()))
        record(db,'AI_INVESTIGATION',{'event_citations':cites,'model':result['model']});db.commit()
    return result

@app.post('/api/checkout')
async def checkout(authorization:str|None=Header(default=None)):
    auth(authorization); base=public_url()
    order=await paypal('POST','/v2/checkout/orders',{
        'intent':'CAPTURE','purchase_units':[{'reference_id':'PROOFPAY-DEMO','description':'Demo managed cloud SLA service',
        'amount':{'currency_code':'USD','value':'200.00'}}],
        'application_context':{'return_url':base+'/checkout/return','cancel_url':base+'/?cancelled=1','user_action':'PAY_NOW'}
    },request_id='proofpay-order-'+str(uuid.uuid4()))
    approval=next((x['href'] for x in order.get('links',[]) if x.get('rel') in ('approve','payer-action')),None)
    if not approval or not order.get('id'):raise HTTPException(502,'PayPal did not provide approval link')
    with lock,conn() as db:
        db.execute('INSERT INTO payments(order_id,state,amount_cents) VALUES(?,?,?)',(order['id'],'CREATED',20000))
        record(db,'ORDER_CREATED',{'order_id':order['id'],'amount':'200.00 USD'});db.commit()
    return {'order_id':order['id'],'approval_url':approval}

@app.get('/checkout/return')
async def checkout_return(token:str=''):
    # Buyer approval is not payment completion. Capture requires explicit server-side PayPal confirmation.
    if not token: return RedirectResponse('/?payment=invalid')
    with lock,conn() as db:
        payment=db.execute('SELECT * FROM payments WHERE order_id=?',(token,)).fetchone()
        if not payment:return RedirectResponse('/?payment=unknown')
        if payment['state']=='CAPTURED':return RedirectResponse('/?payment=success')
        if payment['state']!='CREATED':return RedirectResponse('/?payment=review')
        db.execute('UPDATE payments SET state=? WHERE order_id=?',('CAPTURING',token));db.commit()
    try:
        result=await paypal('POST',f'/v2/checkout/orders/{token}/capture',{},request_id='proofpay-capture-'+token)
        if result.get('status')!='COMPLETED':raise ValueError('capture not completed')
        captures=[c for pu in result.get('purchase_units',[]) for c in pu.get('payments',{}).get('captures',[])]
        capture=next((c for c in captures if c.get('status')=='COMPLETED' and c.get('amount',{}).get('currency_code')=='USD' and c.get('amount',{}).get('value')=='200.00'),None)
        if not capture:raise ValueError('confirmed capture not returned')
    except Exception:
        # Ambiguous state intentionally remains locked for reconciliation. Never blindly re-capture.
        with lock,conn() as db:
            record(db,'CAPTURE_RECONCILIATION_REQUIRED',{'order_id':token});db.commit()
        return RedirectResponse('/?payment=review')
    with lock,conn() as db:
        db.execute('UPDATE payments SET state=?,capture_id=?,approved_at=? WHERE order_id=?',
                   ('CAPTURED',capture['id'],datetime.now(timezone.utc).isoformat(),token))
        record(db,'PAYMENT_CAPTURED',{'order_id':token,'capture_id':capture['id']});db.commit()
    return RedirectResponse('/?payment=success')

@app.post('/api/refund')
async def refund(authorization:str|None=Header(default=None)):
    auth(authorization)
    assessment=calculate()
    if assessment['refund_cents']<=0:raise HTTPException(409,'No contractual refund owed')
    with lock,conn() as db:
        payment=db.execute("SELECT * FROM payments WHERE state='CAPTURED' ORDER BY id DESC LIMIT 1").fetchone()
        review=db.execute('SELECT id FROM investigations ORDER BY id DESC LIMIT 1').fetchone()
        if not review:raise HTTPException(409,'Real AI investigation is required before refund review')
        if not payment or not payment['capture_id']:raise HTTPException(409,'No confirmed PayPal capture')
        if assessment['refund_cents']>payment['amount_cents']:raise HTTPException(409,'Credit exceeds captured payment')
        prior=db.execute('SELECT * FROM refunds WHERE capture_id=?',(payment['capture_id'],)).fetchone()
        if prior:raise HTTPException(409,'Existing refund request: inspect current state; do not duplicate')
        request_id='proofpay-refund-'+str(uuid.uuid4())
        db.execute('INSERT INTO refunds(capture_id,state,amount_cents,request_id) VALUES(?,?,?,?)',
                   (payment['capture_id'],'PENDING',assessment['refund_cents'],request_id))
        record(db,'MERCHANT_REFUND_APPROVED',{'capture_id':payment['capture_id'],'amount_cents':assessment['refund_cents']});db.commit()
    capture_id=payment['capture_id']
    try:
        response=await paypal('POST',f'/v2/payments/captures/{capture_id}/refund',{
            'amount':{'currency_code':'USD','value':f"{Decimal(assessment['refund_cents'])/100:.2f}"},
            'note_to_payer':'Contractual demo SLA service credit'},request_id=request_id)
    except Exception:
        with lock,conn() as db:
            db.execute('UPDATE refunds SET state=? WHERE capture_id=?',('RECONCILE',capture_id))
            record(db,'REFUND_RECONCILIATION_REQUIRED',{'capture_id':capture_id});db.commit()
        raise HTTPException(503,'Refund outcome requires manual PayPal reconciliation; request locked')
    if response.get('status') not in ('COMPLETED','PENDING') or not response.get('id'):
        with lock,conn() as db:
            db.execute('UPDATE refunds SET state=? WHERE capture_id=?',('RECONCILE',capture_id));db.commit()
        raise HTTPException(503,'Unexpected refund response; reconcile before proceeding')
    with lock,conn() as db:
        db.execute('UPDATE refunds SET state=?,paypal_refund_id=? WHERE capture_id=?',(response['status'],response['id'],capture_id))
        record(db,'PAYPAL_REFUND_RESULT',{'capture_id':capture_id,'refund_id':response['id'],'status':response['status']});db.commit()
    return {'paypal_refund_id':response['id'],'status':response['status'],'amount_cents':assessment['refund_cents']}
