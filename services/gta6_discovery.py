"""Non-identifying recipe discovery. Editing never waits for GitHub."""
import base64,hashlib,json,os,re,threading,urllib.request,urllib.error,uuid,struct,zipfile,io
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUTBOX=ROOT/'workspace/gta6-discovery'
CATEGORIES={2054733901,3302890499,2966910054,489117605,719909672}
_lock=threading.Lock()
_scan_slots=threading.BoundedSemaphore(2)
def scan_in_background(data):
    if not _scan_slots.acquire(blocking=False):return
    def worker():
        try:scan_payload(data)
        finally:_scan_slots.release()
    threading.Thread(target=worker,daemon=True).start()
def _arr(v):return v.get('$data',[]) if isinstance(v,dict) else v if isinstance(v,list) else []
def scan_bytes(data):
    if data[:4]!=b'JSAV' or len(data)<24:return []
    version,nc,ns=struct.unpack_from('<III',data,4)
    if version!=3 or nc+ns>1000 or 16+20*(nc+ns)>len(data):return []
    for i in range(nc):
        bid,kind,reserved,off,size=struct.unpack_from('<5I',data,16+20*i)
        if bid!=0xd3f59a5e:continue
        if kind!=1 or reserved or off+size>len(data):return []
        obj=json.loads(data[off:off+size]);invs=obj['$0x7a47cb05']['$0xd3f59a5e']['$0x789bdd41'];out={}
        for inv in invs[:2]:
            if not isinstance(inv,dict):continue
            table=inv.get('$0xc500ddf4',{})
            for spec,cat in zip(_arr(table.get('$0xbe8e255c')),_arr(table.get('$0x12ae4287'))):
                if type(spec) is int and type(cat) is int and cat in CATEGORIES:
                    key=f'{spec&0xffffffff:08x}:{cat:08x}';out[key]={'recipe_id':f'{spec&0xffffffff:08x}','category_id':f'{cat:08x}'}
        return list(out.values())
    return []
def scan_payload(data):
    """Called on load and money outputs. Returns immediately after local queuing."""
    try:
        if data[:2]==b'PK':
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if len(z.infolist())>2048 or sum(i.file_size for i in z.infolist())>512*1024*1024:return
                for n in z.namelist():
                    if not n.endswith('/'):queue_report({'recipes':scan_bytes(z.read(n))})
        else:queue_report({'recipes':scan_bytes(data)})
    except Exception:pass
def queue_report(document):
    if not isinstance(document,dict) or set(document)!={'recipes'}:raise ValueError('Only recipe metadata is accepted')
    rows=document['recipes']
    if not isinstance(rows,list) or len(rows)>512:raise ValueError('Too many recipe entries')
    known=json.loads((ROOT/'data/gta6-known-weapons.json').read_text(encoding='utf8'))['recipes'];clean={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'recipe_id','category_id'}:raise ValueError('Unexpected discovery fields')
        if not all(isinstance(v,str) and re.fullmatch('[0-9a-f]{8}',v) for v in row.values()):raise ValueError('Invalid recipe identifier')
        if int(row['category_id'],16) not in CATEGORIES:continue
        if row['recipe_id'] in known:continue
        clean[row['recipe_id']+':'+row['category_id']]={**row,'status':'unreviewed_weapon_candidate'}
    if not clean:
        if OUTBOX.exists() and os.environ.get('GTA_DISCOVERY_GITHUB_TOKEN') and _lock.acquire(blocking=False):
            threading.Thread(target=_flush_locked,daemon=True).start()
        return 0
    payload={'schema':'twist-it-gta.discovery.v1','candidates':[clean[k] for k in sorted(clean)]}
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode();OUTBOX.mkdir(parents=True,exist_ok=True)
    dest=OUTBOX/(hashlib.sha256(encoded).hexdigest()+'.json')
    if not dest.exists():
        temp=OUTBOX/(uuid.uuid4().hex+'.tmp');temp.write_bytes(encoded);temp.replace(dest)
    # A configured server-side token is required; never accept tokens from reports.
    if os.environ.get('GTA_DISCOVERY_GITHUB_TOKEN') and _lock.acquire(blocking=False):
        threading.Thread(target=_flush_locked,daemon=True).start()
    return len(clean)
def _flush_locked():
    try:flush_pending()
    except Exception:pass  # Keep all reports. A subsequent scan retries synchronization.
    finally:_lock.release()
def flush_pending():
    token=os.environ.get('GTA_DISCOVERY_GITHUB_TOKEN')
    if not token:return False
    repo=os.environ.get('GTA_DISCOVERY_REPO','Joshhooger/gta6-money-editor')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid discovery repository')
    pending=list(OUTBOX.glob('*.json'))
    if not pending:return True
    endpoint='https://api.github.com/repos/'+repo+'/contents/data/weapon-discoveries.json'
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10','User-Agent':'Twist-It-GTA-discovery','Content-Type':'application/json'}
    for attempt in range(3):
        with urllib.request.urlopen(urllib.request.Request(endpoint,headers=headers),timeout=10) as stream:
            response=json.loads(stream.read(3*1024*1024))
        document=json.loads(base64.b64decode(response['content']));known=document.get('candidates',{})
        for p in pending:
            for row in json.loads(p.read_text())['candidates']:known[row['recipe_id']+':'+row['category_id']]=row
        document={'schema':'twist-it-gta.discovery.v1','candidates':dict(sorted(known.items()))}
        content=base64.b64encode((json.dumps(document,indent=2)+'\n').encode()).decode()
        request=urllib.request.Request(endpoint,method='PUT',headers=headers,data=json.dumps({'message':'Collect anonymous weapon recipe candidates','sha':response['sha'],'content':content,'branch':'main'}).encode())
        try:
            with urllib.request.urlopen(request,timeout=10) as result:result.read(1024*1024)
        except urllib.error.HTTPError as error:
            if error.code in (409,422) and attempt<2:continue
            raise
        # Remove only local queue receipts after GitHub accepts the aggregate.
        for p in pending:p.unlink(missing_ok=True)
        return True
    return False
