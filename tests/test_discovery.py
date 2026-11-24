import importlib.util,json,tempfile,os,base64,urllib.error,sys
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
d=module('discovery',root/'services/gta6_discovery.py');money=module('money',root/'services/gta6_money.py')
assert sys.argv[1:],'Pass a local decoded save ZIP fixture'
_,save=money.load(sys.argv[1])[0];raw=bytes(save.data);rows=d.scan_bytes(raw);assert all(set(r)=={'recipe_id','category_id'} for r in rows)
inv=save.inventory(0);table=inv.raw['$0xc500ddf4'];specs=money._arr(table['$0xbe8e255c']);cats=money._arr(table['$0x12ae4287']);index=next(i for i,c in enumerate(cats) if c in d.CATEGORIES);specs[index]=0xdeadbeef;modified=save.to_bytes();rows=d.scan_bytes(modified);assert any(r['recipe_id']=='deadbeef' for r in rows)
with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
 d.OUTBOX=Path(tmp);count=d.queue_report({'recipes':rows});assert count>=1
 files=list(d.OUTBOX.glob('*.json'));assert files
 for file in files:
  payload=json.loads(file.read_text());assert set(payload)=={'schema','candidates'}
  assert all(set(c)=={'recipe_id','category_id','status'} for c in payload['candidates'])
 d.queue_report({'recipes':rows});assert list(d.OUTBOX.glob('*.json'))==files
 try:d.queue_report({'recipes':rows,'account_id':'no'})
 except ValueError:pass
 else:raise AssertionError('Extra metadata must be rejected')
 class Response:
  def __init__(self,b):self.b=b
  def read(self,*a):return self.b
  def __enter__(self):return self
  def __exit__(self,*a):pass
 calls=[]
 def request(req,timeout):
  calls.append(req)
  if req.get_method()=='GET':return Response(json.dumps({'sha':'abc','content':base64.b64encode(json.dumps({'schema':'twist-it-gta.discovery.v1','candidates':{'keep':{'status':'existing'}}}).encode()).decode()}).encode())
  data=json.loads(req.data);doc=json.loads(base64.b64decode(data['content']));assert 'keep' in doc['candidates'];assert any(k.startswith('deadbeef:') for k in doc['candidates']);assert data['sha']=='abc';return Response(b'{}')
 with patch.dict(os.environ,{'GTA_DISCOVERY_GITHUB_TOKEN':'test-only'}),patch.object(d.urllib.request,'urlopen',request):assert d.flush_pending()
 assert len(calls)==2 and not list(d.OUTBOX.glob('*.json'))
 d.queue_report({'recipes':rows})
 with patch.dict(os.environ,{'GTA_DISCOVERY_GITHUB_TOKEN':'test-only'}),patch.object(d.urllib.request,'urlopen',side_effect=OSError('offline')):
  try:d.flush_pending()
  except OSError:pass
 assert list(d.OUTBOX.glob('*.json'))
assert bytes(money.load(sys.argv[1])[0][1].data)==raw
print(json.dumps({'passed':True,'checks':8,'real_network_calls':0}))
