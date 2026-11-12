"""Song Manager 1.6: chorus over HTTP (info, set, remove, add song with chorus, save, re-read) + dialog in a browser."""
import sys,os,json,time,re,subprocess,urllib.request,urllib.error,shutil
sys.path.insert(0,'/home/claude')
S=sys.argv[1]
PORT=8793; srv=subprocess.Popen([sys.executable,'songmgr.py','--no-browser','--port',str(PORT)],cwd='/home/claude',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
time.sleep(1.5); base='http://127.0.0.1:%d'%PORT
ok=[];bad=[]
def chk(n,c,x=''): (ok if c else bad).append(n); print(('PASS ' if c else 'FAIL ')+n+('' if c else '  '+str(x)[:300]),flush=True)
def req(path,body=None,tok=None,raw=False):
    h={'X-Token':tok} if tok else {}
    data=None
    if body is not None: data=json.dumps(body).encode(); h['Content-Type']='application/json'
    try:
        r=urllib.request.urlopen(urllib.request.Request(base+path,data=data,headers=h),timeout=600); b=r.read(); return r.status,(b if raw else json.loads(b))
    except urllib.error.HTTPError as e:
        b=e.read()
        try: return e.code,json.loads(b)
        except Exception: return e.code,b
try:
    s,page=req('/',raw=True); tok=re.search(rb'const TOKEN="([0-9a-f]+)"',page).group(1).decode()
    s,i=req('/api/open',{'path':'/home/claude/test_mppv45.iso'},tok); chk('open ISO',s==200,i)
    s,c=req('/api/chorusinfo',{'number':3},tok); chk('chorus info of a disc chorus song',c['chorus'] and abs(c['at']-8.15)<0.1,c)
    s,c=req('/api/chorusinfo',{'number':9},tok); chk('chorus info: none',not c['chorus'],c)
    s,r=req('/api/setchorus',{'number':9,'path':S+'/vocal.wav','at':3.5},tok); chk('add chorus to a disc song',s==200 and r['song']['chorus'] and r['song']['chorus_changed'] and abs(r['chorus']['at']-3.5)<0.01,r)
    s,r=req('/api/setchorus',{'number':1,'path':None},tok); chk('remove a disc chorus',s==200 and not r['song']['chorus'] and r['info']['pending']['chorus']==2,r)
    s,r=req('/api/setchorus',{'number':5,'path':'/nonexistent.wav'},tok); chk('missing audio file refused',s!=200 or 'error' in r,r)
    # add a song with a chorus (auto-picked next to the MIDI)
    os.makedirs(S+'/addch',exist_ok=True)
    import songdisc; d=songdisc.KaraokeDisc('/home/claude/test_mppv45.iso'); open(S+'/addch/my song.mid','wb').write(d.midi(d.find(5))); d.close()
    shutil.copy(S+'/vocal.wav',S+'/addch/my song chorus.wav')
    s,m=req('/api/midimeta',{'paths':[S+'/addch/my song.mid']},tok); it=m['items'][0]
    chk('chorus file picked automatically',it['chorus'].endswith('my song chorus.wav'),it)
    it['number']=98111; it['chorus_at']=1.25
    s,a=req('/api/add',{'items':[it]},tok); chk('add song with chorus',s==200 and a['added'] and a['added'][0]['chorus'],a)
    out=S+'/mgr_chorus.iso'
    if os.path.exists(out): os.remove(out)
    s,j=req('/api/save',{'out':out},tok)
    while True:
        s,j=req('/api/job',tok=tok)
        if j['state']!='running': break
        time.sleep(0.5)
    chk('saved',j['state']=='done',j)
    d2=songdisc.KaraokeDisc(out)
    t=lambda n: songdisc.chorus_trigger(d2.midi(d2.find(n)))
    chk('re-read: disc song now has chorus at 3.5 s',d2.find(9).has_audio and abs(t(9)-3.5)<0.05 and len(d2.chorus(d2.find(9)))==1)
    chk('re-read: removed chorus gone',not d2.find(1).has_audio and t(1) is None)
    chk('re-read: new song with chorus at 1.25 s',d2.find(98111).has_audio and abs(t(98111)-1.25)<0.05)
    d2.close()
    # dialog renders
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch(); pg=b.new_page(viewport={'width':1280,'height':900}); errs=[]
        pg.on('pageerror',lambda e: errs.append(str(e)))
        pg.goto(base+'/'); time.sleep(1.5)
        pg.fill('#q','birthday'); pg.keyboard.press('Enter'); time.sleep(1.5)
        btn=pg.query_selector('button.chs'); chk('row has a chorus button',btn is not None)
        if btn: btn.click(); time.sleep(1.2); chk('chorus dialog opens with info','chorus' in pg.inner_text('#chcur').lower(),pg.inner_text('#chcur'))
        pg.screenshot(path=S+'/mgr_chorus.png')
        chk('no page errors',not errs,errs)
        b.close()
finally:
    srv.terminate()
print('%d passed, %d failed'%(len(ok),len(bad)),bad)
