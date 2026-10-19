"""Verify management errors remain local without disclosing exception details."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['APP_ENV']='test'
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from app.error_redirect import error_redirect_middleware
app=FastAPI()
app.middleware('http')(error_redirect_middleware)
@app.get('/admin/subscriptions/error')
def error(): raise RuntimeError('sensitive-diagnostic-must-not-leak')
@app.get('/admin/subscriptions/denied')
def denied(): return Response(status_code=403)
@app.get('/sub/invalid')
def invalid(): return Response(status_code=404)
@app.get('/unrelated')
def unrelated(): return Response(status_code=403)
client=TestClient(app,follow_redirects=False)
for accept in ['text/html','application/json']:
 r=client.get('/admin/subscriptions/error',headers={'accept':accept})
 assert r.status_code==500 and 'location' not in r.headers
 assert 'no-store' in r.headers['cache-control']
 assert 'sensitive-diagnostic' not in r.text
 assert ('返回订阅管理' in r.text) if accept=='text/html' else r.json()['error']=='subscription_operation_failed'
r=client.get('/admin/subscriptions/denied');assert r.status_code==403 and 'location' not in r.headers
r=client.get('/sub/invalid');assert r.status_code==404 and 'location' not in r.headers
r=client.get('/unrelated');assert r.status_code==403 and 'location' not in r.headers
print('SUBSCRIPTION_MANAGEMENT_ERRORS=PASS (5 checks)')
