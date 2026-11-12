import sys,os,json,time,re,subprocess,urllib.request,urllib.error,shutil
sys.path.insert(0,'/home/claude')
PORT=8791; srv=subprocess.Popen([sys.executable,'songmgr.py','--no-browser','--port',str(PORT)],cwd='/home/claude',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
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
def wait_job(tok):
    while True:
        s,j=req('/api/job',tok=tok)
        if j['state']!='running': return j
        time.sleep(0.5)
try:
    s,page=req('/',raw=True); tok=re.search(rb'const TOKEN="([0-9a-f]+)"',page).group(1).decode()
    chk('page + token',s==200 and len(tok)==32)
    chk('no token -> 403',req('/api/info')[0]==403)
    s,i=req('/api/open',{'path':'/home/claude/minidisc'},tok); chk('open folder',s==200 and i['songs']==17142 and i['kind']=='folder',i)
    s,l=req('/api/songs?q=lupang',tok=tok); chk('search',s==200 and l['total']>=2 and l['songs'][0]['title']=='Lupang Hinirang',l)
    s,l=req('/api/songs?q=005810',tok=tok); chk('search by number',l['songs'] and l['songs'][0]['title']=='All Of A Sudden',l)
    shutil.rmtree('/home/claude/mgr_extract',ignore_errors=True)
    s,j=req('/api/extract',{'numbers':[1,3,5],'folder':'/home/claude/mgr_extract'},tok); j=wait_job(tok)
    files=sorted(os.listdir('/home/claude/mgr_extract')) if os.path.isdir('/home/claude/mgr_extract') else []
    chk('extract 3 songs (+chorus)',j['state']=='done' and sum(f.endswith('.mid') for f in files)==3 and sum(f.endswith('.wav') for f in files)==2,(j,files))
    s,r=req('/api/chorus?n=1&i=0',tok=tok,raw=True); chk('chorus preview is WAV',s==200 and r[:4]==b'RIFF',r[:60])
    mid=[f for f in files if f.startswith('000005')][0]
    s,m=req('/api/midimeta',{'paths':['/home/claude/mgr_extract/'+mid,'/home/claude/minidisc/KARA125/PA.bin']},tok)
    chk('midi meta',m['items'][0]['ok'] and m['items'][0]['title']=='Wedding March' and not m['items'][1]['ok'],m)
    it=m['items'][0]; it['number']=99501; it['title']='My New Song'
    s,a=req('/api/add',{'items':[it]},tok); chk('add song',s==200 and a['added'] and a['info']['pending']['added']==1,a)
    s,a=req('/api/add',{'items':[it]},tok); chk('duplicate number rejected',not a['added'] and a['errors'],a)
    s,dd=req('/api/delete',{'numbers':[2]},tok); chk('delete',dd['done']==1 and dd['info']['pending']['deleted']==1,dd)
    s,l=req('/api/songs?q=99501',tok=tok); chk('added song listed',l['songs'][0]['origin']=='added' and l['songs'][0]['title']=='My New Song',l)
    shutil.rmtree('/home/claude/minidisc_out',ignore_errors=True)
    s,j=req('/api/save',{'out':'/home/claude/minidisc_out'},tok); j=wait_job(tok); chk('save folder',j['state']=='done',j)
    s,i=req('/api/open',{'path':'/home/claude/minidisc_out'},tok)
    s,l=req('/api/songs?q=99501',tok=tok); chk('reopened: added song present',l['songs'] and l['songs'][0]['title']=='My New Song',l)
    s,l=req('/api/songs?q=Lupang',tok=tok); chk('reopened: deleted song gone',[x['number'] for x in l['songs']]==[1],l)
    chk('new bank file written',os.path.exists('/home/claude/minidisc_out/KARA125/AUDIO2.DA2'))
    s,snd=req('/api/sound',{'sound':'live','device':'capture'},tok); chk('MIDI player: choose device',s==200 and snd['sound']=='live',snd)
    s,mp=req('/api/mplay',{'number':5},tok); time.sleep(1.5); s,st=req('/api/sound',tok=tok)
    chk('MIDI player: playing',st['player']['state']=='playing' and st['player']['pos']>0.5 and st['player']['number']==5,st)
    s,st=req('/api/mpause',{},tok); chk('MIDI player: pause',st['player']['state']=='paused',st)
    s,st=req('/api/mpause',{},tok); s,st=req('/api/mplay',{'number':5,'start':30},tok); time.sleep(0.5); s,st=req('/api/sound',tok=tok)
    chk('MIDI player: seek',st['player']['pos']>=30,st)
    s,st=req('/api/mstop',{},tok); chk('MIDI player: stop',st['player']['state']=='idle',st)
    req('/api/sound',{'sound':'render'},tok)
    s,p=req('/api/play',{'number':99501},tok)
    for k in range(120):
        s,p=req('/api/preview',tok=tok)
        if p['state']!='rendering': break
        time.sleep(1)
    chk('preview render',p['state']=='ready',p)
    s,r=req('/api/save',{'out':'/home/claude/minidisc_out'},tok); j=wait_job(tok) if s==200 else {'state':'error'}
    chk('refuses to write over existing folder / original',j['state']=='error' or s!=200,(s,r,j))
    # ---- music videos (v1.3)
    S='/tmp/claude-0/-home-claude/126d18a8-642d-5ea0-a795-8dbb1cf4e033/scratchpad'
    s,i=req('/api/open',{'path':'/home/claude/test_mppv45.iso'},tok); chk('open ISO with MVV',s==200 and i['has_mvv'] and i['mvs']==153,i)
    s,l=req('/api/songs?only=mv',tok=tok); chk('filter: music videos',l['total']==153 and all(x['mv'] for x in l['songs']) and l['songs'][0]['mvv'],l['total'])
    s,l=req('/api/songs?only=songs&q=90034',tok=tok); chk('filter: songs excludes MVs',l['total']==0,l)
    s,r=req('/api/mplay',{'number':90034},tok); chk('MIDI play of an MV refused',s==400,r)
    s,m=req('/api/videometa',{'paths':[S+'/clip.mp4',S+'/99501.mvv',S+'/nothing.mp4']},tok)
    it=m['items']; chk('video meta',it[0]['ok'] and it[0]['convert'] and it[1]['ok'] and it[1]['as_is'] and not it[2]['ok'] and it[0]['number']>91782 and it[0]['number']!=it[1]['number'],it)
    it[0]['title']='Converted One'; it[1]['title']='As Is'; it[1]['artist']='Someone'
    s,j=req('/api/addvideo',{'items':it[:2]},tok); j=wait_job(tok)
    chk('add videos job',j['state']=='done' and j['result']['added']==2 and j['result']['converted']==1,j)
    s,i=req('/api/info',tok=tok); chk('pending shows MVs',i['pending']['added_mv']==2 and i['pending']['mv_bytes']>0,i['pending'])
    n0=it[0]['number']
    s,l=req('/api/songs?q=Converted%20One',tok=tok); chk('added MV listed',l['songs'] and l['songs'][0]['mv'] and l['songs'][0]['origin']=='added',l)
    s,dd=req('/api/delete',{'numbers':[90266]},tok)
    out=S+'/mgr_mv.iso'
    if os.path.exists(out): os.remove(out)
    s,j=req('/api/save',{'out':out},tok); j=wait_job(tok); chk('save ISO with MVs',j['state']=='done',j)
    s,i=req('/api/open',{'path':out},tok); s,l=req('/api/songs?only=mv&q='+str(n0),tok=tok)
    chk('reopened: new MV with its file',l['songs'] and l['songs'][0]['mvv'] and l['songs'][0]['mvv'].lower()=='%05d.mvv'%n0,l)
    s,l=req('/api/songs?q=90266',tok=tok); chk('reopened: deleted MV gone',l['total']==0,l)
    shutil.rmtree(S+'/mvx2',ignore_errors=True)
    s,j=req('/api/extract',{'numbers':[n0,90261],'folder':S+'/mvx2'},tok); j=wait_job(tok)
    fs=sorted(os.listdir(S+'/mvx2')); chk('extract MVs as .avi',j['state']=='done' and sum(f.endswith('.avi') for f in fs)==2,(j,fs))
    s,r=req('/api/openvideo',{'number':n0},tok); chk('open video (copies to a temp .avi)',s==200 and r['path'].endswith('.avi') and os.path.getsize(r['path'])>1000,r)
    os.remove(out)
    # ---- music-video lyrics (v1.4)
    s,i=req('/api/open',{'path':'/home/claude/test_mppv45.iso'},tok)
    s,li=req('/api/lyricsinfo',{'path':S+'/lyr.mid'},tok); chk('lyrics info',s==200 and li['syllables']==164 and li['lines']>30,li)
    s,r=req('/api/lyricsinfo',{'path':S+'/99501.mvv'},tok); chk('lyrics info: not a MIDI -> error',s==400,r)
    s,r=req('/api/setlyrics',{'number':90034,'path':S+'/lyr.mid','offset':1.0},tok); chk('set lyrics on a disc MV',s==200 and r['song']['lyrics'] and r['info']['pending']['lyrics']==1,r)
    s,t=req('/api/lyricstext',{'number':90034},tok); chk('lyrics text (timed lines)',t['lines'] and t['lines'][0]['t']==3.4 and t['notes']==0,t['lines'][:2])
    s,m=req('/api/videometa',{'paths':[S+'/clip.mp4']},tok); chk('lyrics file next to the video found',m['items'][0]['lyrics'].endswith('clip.mid'),m)
    it=m['items'][0]; it['title']='Video With Lyrics'; it['offset']=0
    s,j=req('/api/addvideo',{'items':[it]},tok); j=wait_job(tok); chk('add video with lyrics',j['state']=='done' and j['result']['added']==1,j)
    nl=it['number']
    out=S+'/mgr_lyr.iso'
    if os.path.exists(out): os.remove(out)
    s,j=req('/api/save',{'out':out},tok); j=wait_job(tok); chk('save with lyrics',j['state']=='done',j)
    s,i=req('/api/open',{'path':out},tok); chk('reopened: 2 MVs with lyrics',i['mv_lyrics']==2,i.get('mv_lyrics'))
    s,l=req('/api/songs?only=mv&q='+str(nl),tok=tok); chk('reopened: new MV has lyrics',l['songs'][0]['lyrics'],l)
    s,r=req('/api/setlyrics',{'number':90034,'path':None},tok); chk('remove lyrics',s==200 and not r['song']['lyrics'],r)
    os.remove(out)
finally:
    srv.terminate()
print(len(ok),'passed,',len(bad),'failed')
