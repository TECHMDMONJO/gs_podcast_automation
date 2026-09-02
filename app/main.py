import os, re, json, time, base64, hashlib, secrets, sqlite3, asyncio, subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydub import AudioSegment, effects
from gtts import gTTS

try:
    import edge_tts
except Exception:
    edge_tts = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MEDIA = ROOT / "media"
DATA.mkdir(exist_ok=True); MEDIA.mkdir(exist_ok=True)
DB = DATA / "podcast.db"
APP_NAME = "GS Podcast Studio"
SECRET = os.getenv("APP_SECRET", "change-this-secret")

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")

PROVIDERS = {
    "Gemini": {"base": "gemini", "default": "gemini-3.7-flash", "models": ["gemini-3.7-flash","gemini-3.6-flash","gemini-3.5-flash","gemini-3.5-flash-lite"]},
    "Groq": {"base": "groq", "default": "openai/gpt-oss-20b", "models": ["openai/gpt-oss-20b","openai/gpt-oss-120b"]},
    "OpenRouter Free": {"base": "openrouter", "default": "openrouter/free", "models": ["openrouter/free"]},
    "OpenAI": {"base": "openai", "default": "gpt-5.6-luna", "models": ["gpt-5.6-luna","gpt-5.6-terra","gpt-5.6-sol"]},
    "Ollama Local": {"base": "ollama", "default": "llama3.2", "models": ["llama3.2","qwen2.5:7b","mistral"]},
}
VOICE_MAP = {
    "English - US": {"Male":"en-US-GuyNeural","Female":"en-US-JennyNeural"},
    "English - UK": {"Male":"en-GB-RyanNeural","Female":"en-GB-SoniaNeural"},
    "Swahili - Kenya": {"Male":"sw-KE-RafikiNeural","Female":"sw-KE-ZuriNeural"},
    "Spanish": {"Male":"es-ES-AlvaroNeural","Female":"es-ES-ElviraNeural"},
    "French": {"Male":"fr-FR-HenriNeural","Female":"fr-FR-DeniseNeural"},
}

# ---------- database ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, email TEXT, is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1, banned INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, value TEXT, UNIQUE(user_id,key));
    CREATE TABLE IF NOT EXISTS podcasts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, description TEXT, provider TEXT, model TEXT, mp3 TEXT, wav TEXT, cover TEXT, duration_ms INTEGER DEFAULT 0, chapters TEXT DEFAULT '[]', created_at TEXT);
    CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, status TEXT, details TEXT, created_at TEXT);
    ''')
    if not c.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        c.execute("INSERT INTO users(username,password,email,is_admin,created_at) VALUES(?,?,?,?,?)",('admin',pw_hash('smbagathi'),'admin@local',1,now()))
    c.commit(); c.close()

def now(): return datetime.utcnow().isoformat(timespec='seconds')+'Z'
def pw_hash(p): return hashlib.sha256((SECRET+'|'+p).encode()).hexdigest()
def pw_ok(p,h): return secrets.compare_digest(pw_hash(p),h)
def set_setting(uid,k,v):
    c=db(); c.execute("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",(uid,k,v)); c.commit(); c.close()
def get_setting(uid,k,default=''):
    c=db(); r=c.execute("SELECT value FROM settings WHERE user_id=? AND key=?",(uid,k)).fetchone(); c.close(); return r['value'] if r else default
def log(uid,a,s,d=''):
    c=db(); c.execute("INSERT INTO logs(user_id,action,status,details,created_at) VALUES(?,?,?,?,?)",(uid,a,s,d[:4000],now())); c.commit(); c.close()

# ---------- sessions ----------
SESSIONS={}
def current_user(request: Request):
    sid=request.cookies.get('gpsid')
    uid=SESSIONS.get(sid)
    if not uid: return None
    c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
    if not u or not u['active'] or u['banned']: return None
    return u

def require_user(request):
    u=current_user(request)
    if not u: raise HTTPException(401,'Please log in.')
    return u

# ---------- models ----------
class Auth(BaseModel): username:str; password:str; email:str=''
class SettingsPayload(BaseModel): provider:str; model:str; gemini_keys:str=''; groq_keys:str=''; openrouter_keys:str=''; openai_keys:str=''; ollama_url:str='http://127.0.0.1:11434'
class TestPayload(BaseModel): provider:str; model:str; key:str=''
class RewritePayload(BaseModel): text:str; provider:str; model:str; language:str='English'; instruction:str='Rewrite this into a natural podcast script with two speakers.'
class GeneratePayload(BaseModel):
    text:str
    provider:str='Gemini'; model:str='gemini-3.7-flash'; language:str='English - US'; speed:float=1.0
    speakers:int=2
    speaker_names:list[str]=[]
    speaker_genders:list[str]=[]
    intro:str=''; outro:str=''; background:bool=True; background_volume:float=0.10
    export_wav:bool=True; ai_metadata:bool=True

# ---------- AI ----------
def keys_for(uid,provider):
    kmap={'Gemini':'gemini_keys','Groq':'groq_keys','OpenRouter Free':'openrouter_keys','OpenAI':'openai_keys'}
    if provider=='Ollama Local': return ['']
    raw=get_setting(uid,kmap.get(provider,''),'')
    return [x.strip() for x in re.split(r'[,\n]+',raw) if x.strip()]

def clean_json_text(s):
    s=s.strip()
    s=re.sub(r'^```(?:json)?\s*','',s,flags=re.I); s=re.sub(r'\s*```$','',s)
    return s.strip()

def gemini(key,model,prompt):
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    r=requests.post(url,params={'key':key},json={'contents':[{'parts':[{'text':prompt}]}]},timeout=90)
    if r.status_code>=400: raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:600]}")
    j=r.json(); return j['candidates'][0]['content']['parts'][0]['text']

def compat(base,key,model,prompt,headers=None):
    h={'Authorization':f'Bearer {key}','Content-Type':'application/json'}; h.update(headers or {})
    r=requests.post(base+'/chat/completions',headers=h,json={'model':model,'messages':[{'role':'user','content':prompt}],'temperature':0.7},timeout=90)
    if r.status_code>=400: raise RuntimeError(f"HTTP {r.status_code}: {r.text[:600]}")
    j=r.json(); return j['choices'][0]['message']['content']

def ollama(prompt,model,url):
    r=requests.post(url.rstrip('/')+'/api/chat',json={'model':model,'messages':[{'role':'user','content':prompt}], 'stream':False},timeout=120)
    if r.status_code>=400: raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text[:600]}")
    return r.json()['message']['content']

def ai(uid,provider,model,prompt):
    errs=[]
    if provider not in PROVIDERS: raise RuntimeError('Unknown AI provider')
    if provider=='Ollama Local':
        try: return ollama(prompt,model,get_setting(uid,'ollama_url','http://127.0.0.1:11434'))
        except Exception as e: raise RuntimeError(str(e))
    for i,k in enumerate(keys_for(uid,provider),1):
        try:
            if provider=='Gemini': return gemini(k,model,prompt)
            if provider=='Groq': return compat('https://api.groq.com/openai/v1',k,model,prompt)
            if provider=='OpenRouter Free': return compat('https://openrouter.ai/api/v1',k,model,prompt,{'HTTP-Referer':'https://example.com','X-Title':APP_NAME})
            if provider=='OpenAI': return compat('https://api.openai.com/v1',k,model,prompt)
        except Exception as e: errs.append(f'key {i}: {e}')
    raise RuntimeError(' | '.join(errs) if errs else f'No API key configured for {provider}.')

# ---------- audio ----------
def voice_for(lang,gender):
    opts=VOICE_MAP.get(lang,VOICE_MAP['English - US'])
    return opts.get(gender, list(opts.values())[0])

def edge_segment(text,path,voice,speed):
    async def run():
        rate=f'{int((speed-1)*100):+d}%'
        await edge_tts.Communicate(text,voice,rate=rate).save(str(path))
    asyncio.run(run())

def tts_segment(text,path,lang):
    code={'English - US':'en','English - UK':'en','Swahili - Kenya':'sw','Spanish':'es','French':'fr'}.get(lang,'en')
    gTTS(text=text,lang=code,slow=False).save(str(path))

def synth(text,path,voice,gender,speed,lang):
    if edge_tts:
        try: edge_segment(text,path,voice,speed); return
        except Exception: pass
    tts_segment(text,path,lang)

def parse_dialogue(text,names,speakers):
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    result=[]; current=0
    pat=re.compile(r'^(?:speaker\s*)?(\d)\s*[:\-]\s*(.+)$',re.I)
    for line in lines:
        m=pat.match(line)
        if m:
            idx=max(0,min(int(m.group(1))-1,speakers-1)); result.append((idx,m.group(2).strip()))
        else:
            result.append((current,line)); current=(current+1)%speakers
    if not result: result=[(0,text)]
    return result

def ambient(duration_ms,vol):
    # soft, non-musical background pad; avoids bundling copyrighted music.
    a=AudioSegment.silent(duration=duration_ms)
    for freq in (174,220):
        tone=AudioSegment.silent(duration=duration_ms)
        from pydub.generators import Sine
        tone=Sine(freq).to_audio_segment(duration=duration_ms).apply_gain(-42 + 20*max(0,min(vol,0.4)))
        a=a.overlay(tone)
    return a

def make_cover(title,path):
    if Image is None: return ''
    im=Image.new('RGB',(1400,1400),(18,22,32)); d=ImageDraw.Draw(im)
    # layered circles and lines for a clean original cover
    for r in range(120,700,100): d.ellipse((700-r,700-r,700+r,700+r),outline=(70,80,100),width=5)
    try: font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',80); small=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',36)
    except: font=small=None
    words=[]; line=''
    for w in title.split():
        if len(line)+len(w)>22: words.append(line); line=''
        line += (' ' if line else '')+w
    if line: words.append(line)
    y=500
    for w in words[:5]:
        box=d.textbbox((0,0),w,font=font); x=(1400-(box[2]-box[0]))//2; d.text((x,y),w,font=font,fill='white'); y+=100
    d.text((70,1290),'GS PODCAST STUDIO',font=small,fill=(170,180,195))
    im.save(path,quality=92); return str(path)

def heuristic_meta(source):
    first=re.sub(r'\s+',' ',source).strip()[:100]
    title=first.split('.')[0][:70] or 'New Podcast Episode'
    return title, f"A podcast episode exploring {title}. Created with GS Podcast Studio."

def create_audio(uid,p):
    if not p.text.strip(): raise RuntimeError('Enter a topic, script, article, notes, or dialogue first.')
    names=(p.speaker_names+['Speaker 1','Speaker 2','Speaker 3','Speaker 4'])[:p.speakers]
    genders=(p.speaker_genders+['Male','Female','Male','Female'])[:p.speakers]
    source=p.text.strip()
    # AI rewrite produces clean speaker dialogue when enabled.
    if p.ai_metadata or p.provider:
        prompt=("You are a professional podcast producer. Transform the supplied material into a concise, natural, "
                f"fact-conscious {p.language} podcast script for {p.speakers} speakers. "
                "Label every spoken line exactly as Speaker 1:, Speaker 2:, etc. Keep useful facts, do not invent citations, "
                "and make it engaging.\n\nMATERIAL:\n"+source[:30000])
        try: script=ai(uid,p.provider,p.model,prompt)
        except Exception: script=source
    else: script=source
    title,desc=heuristic_meta(source)
    if p.ai_metadata:
        try:
            m=ai(uid,p.provider,p.model,"Return ONLY JSON with keys title and description. Create a compelling podcast title and 1-2 sentence description from this material:\n"+source[:10000])
            j=json.loads(clean_json_text(m)); title=str(j.get('title') or title)[:180]; desc=str(j.get('description') or desc)[:500]
        except Exception: pass
    safe=re.sub(r'[^a-zA-Z0-9_-]+','_',title)[:60] or 'episode'
    stamp=int(time.time())
    work=MEDIA/f'tmp_{uid}_{stamp}'; work.mkdir(exist_ok=True)
    dialogue=parse_dialogue(script,names,p.speakers)
    final=AudioSegment.empty(); chapters=[]; cursor=0
    if p.intro.strip():
        ip=work/'intro.mp3'; synth(p.intro.strip(),ip,voice_for(p.language,'Female'), 'Female',p.speed,p.language); seg=AudioSegment.from_file(ip); final+=seg; cursor+=len(seg)
    for idx,text in dialogue:
        fp=work/f's{idx}_{len(chapters)}.mp3'; synth(text,fp,voice_for(p.language,genders[idx]),genders[idx],p.speed,p.language)
        seg=AudioSegment.from_file(fp)
        start=cursor; final+=seg; cursor+=len(seg)
        chapters.append({'start_ms':start,'speaker':names[idx],'title':text[:70]})
        final+=AudioSegment.silent(duration=120)
    if p.outro.strip():
        op=work/'outro.mp3'; synth(p.outro.strip(),op,voice_for(p.language,'Male'),'Male',p.speed,p.language); seg=AudioSegment.from_file(op); final+=seg; cursor+=len(seg)
    final=effects.normalize(final)
    if p.background:
        bed=ambient(len(final),p.background_volume); final=final.overlay(bed)
    mp3=MEDIA/f'{safe}_{stamp}.mp3'; final.export(mp3,format='mp3',bitrate='160k')
    wav=''
    if p.export_wav:
        wp=MEDIA/f'{safe}_{stamp}.wav'; final.export(wp,format='wav'); wav=str(wp)
    cover=MEDIA/f'{safe}_{stamp}.jpg'; make_cover(title,cover)
    # Persist paths relative to media directory.
    c=db(); c.execute('INSERT INTO podcasts(user_id,title,description,provider,model,mp3,wav,cover,duration_ms,chapters,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uid,title,desc,p.provider,p.model,mp3.name,Path(wav).name if wav else '',cover.name,len(final),json.dumps(chapters),now())); pid=c.lastrowid; c.commit(); c.close()
    try:
        for x in work.iterdir(): x.unlink()
        work.rmdir()
    except: pass
    return {'id':pid,'title':title,'description':desc,'duration_ms':len(final),'chapters':chapters}

# ---------- routes ----------
@app.on_event('startup')
def startup(): init_db()

@app.get('/',response_class=HTMLResponse)
def index():
    return (ROOT/'app'/'static'/'index.html').read_text()

@app.post('/api/register')
def register(a:Auth):
    if len(a.username)<3 or len(a.password)<4: raise HTTPException(400,'Username/password too short.')
    c=db()
    try: c.execute('INSERT INTO users(username,password,email,created_at) VALUES(?,?,?,?)',(a.username,pw_hash(a.password),a.email,now())); c.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,'Username already exists.')
    finally: c.close()
    return {'ok':True}

@app.post('/api/login')
def login(a:Auth):
    c=db(); u=c.execute('SELECT * FROM users WHERE username=?',(a.username,)).fetchone(); c.close()
    if not u or not pw_ok(a.password,u['password']): raise HTTPException(401,'Invalid username or password.')
    if not u['active'] or u['banned']: raise HTTPException(403,'Account is inactive or banned.')
    sid=secrets.token_urlsafe(32); SESSIONS[sid]=u['id']; log(u['id'],'Login','Success')
    r=JSONResponse({'ok':True,'user':{'username':u['username'],'admin':bool(u['is_admin'])}}); r.set_cookie('gpsid',sid,httponly=True,samesite='lax',max_age=86400); return r

@app.post('/api/logout')
def logout(request:Request):
    sid=request.cookies.get('gpsid'); uid=SESSIONS.pop(sid,None) if sid else None
    if uid: log(uid,'Logout','Success')
    r=JSONResponse({'ok':True}); r.delete_cookie('gpsid'); return r

@app.get('/api/me')
def me(request:Request):
    u=current_user(request)
    if not u: return {'authenticated':False}
    return {'authenticated':True,'username':u['username'],'admin':bool(u['is_admin'])}

@app.get('/api/settings')
def settings(request:Request):
    u=require_user(request); out={}
    for k in ['gemini_keys','groq_keys','openrouter_keys','openai_keys','ollama_url']:
        out[k]=get_setting(u['id'],k,'')
    out['provider']=get_setting(u['id'],'provider','Gemini'); out['model']=get_setting(u['id'],'model','gemini-3.7-flash')
    return out

@app.post('/api/settings')
def save_settings(request:Request,p:SettingsPayload):
    u=require_user(request)
    for k in ['gemini_keys','groq_keys','openrouter_keys','openai_keys','ollama_url']: set_setting(u['id'],k,getattr(p,k))
    set_setting(u['id'],'provider',p.provider); set_setting(u['id'],'model',p.model); log(u['id'],'Settings','Success'); return {'ok':True}

@app.post('/api/test')
def test(request:Request,p:TestPayload):
    u=require_user(request); provider=p.provider; model=p.model or PROVIDERS[provider]['default']
    try:
        if provider=='Ollama Local': ans=ollama('Reply with exactly: CONNECTION OK',model,get_setting(u['id'],'ollama_url','http://127.0.0.1:11434'))
        else:
            k=p.key.strip() or (keys_for(u['id'],provider)[0] if keys_for(u['id'],provider) else '')
            if not k: return {'ok':False,'detail':'No API key supplied.'}
            ans=ai(u['id'],provider,model,'Reply with exactly: CONNECTION OK')
        log(u['id'],'AI Test','Success',provider+' / '+model); return {'ok':True,'detail':'Connection OK: '+ans[:100]}
    except Exception as e:
        log(u['id'],'AI Test','Failed',str(e)); return {'ok':False,'detail':str(e)}

@app.post('/api/rewrite')
def rewrite(request:Request,p:RewritePayload):
    u=require_user(request)
    if not p.text.strip(): raise HTTPException(400,'Text is empty.')
    prompt=f"{p.instruction}\nLanguage: {p.language}\nMaterial:\n{p.text[:30000]}\nReturn only the finished script."
    try: out=ai(u['id'],p.provider,p.model,prompt); return {'ok':True,'text':out}
    except Exception as e: raise HTTPException(502,str(e))

@app.post('/api/generate')
def generate(request:Request,p:GeneratePayload):
    u=require_user(request)
    if p.speakers<1 or p.speakers>4: raise HTTPException(400,'Speakers must be 1-4.')
    try:
        result=create_audio(u['id'],p); log(u['id'],'Generate Podcast','Success',result['title']); return result
    except Exception as e:
        log(u['id'],'Generate Podcast','Failed',str(e)); raise HTTPException(502,str(e))

@app.get('/api/podcasts')
def podcasts(request:Request):
    u=require_user(request); c=db(); rows=c.execute('SELECT * FROM podcasts WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall(); c.close()
    return [dict(r) for r in rows]

@app.get('/api/media/{filename}')
def media(request:Request,filename:str):
    u=require_user(request); c=db(); r=c.execute('SELECT 1 FROM podcasts WHERE user_id=? AND (mp3=? OR wav=? OR cover=?)',(u['id'],filename,filename,filename)).fetchone(); c.close()
    if not r: raise HTTPException(404,'Not found')
    path=MEDIA/filename
    if not path.exists(): raise HTTPException(404,'Media no longer exists')
    return FileResponse(path)

@app.delete('/api/podcasts/{pid}')
def delete_podcast(request:Request,pid:int):
    u=require_user(request); c=db(); r=c.execute('SELECT * FROM podcasts WHERE id=? AND user_id=?',(pid,u['id'])).fetchone()
    if not r: raise HTTPException(404,'Episode not found')
    for k in ['mp3','wav','cover']:
        if r[k]:
            try: (MEDIA/r[k]).unlink(missing_ok=True)
            except: pass
    c.execute('DELETE FROM podcasts WHERE id=?',(pid,)); c.commit(); c.close(); log(u['id'],'Delete Podcast','Success',str(pid)); return {'ok':True}

@app.get('/api/logs')
def logs(request:Request):
    u=require_user(request); c=db(); rows=c.execute('SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 200',(u['id'],)).fetchall(); c.close(); return [dict(r) for r in rows]

@app.get('/api/admin/users')
def admin_users(request:Request):
    u=require_user(request)
    if not u['is_admin']: raise HTTPException(403,'Admin only')
    c=db(); rows=c.execute('SELECT id,username,email,is_admin,active,banned,created_at FROM users ORDER BY id').fetchall(); c.close(); return [dict(r) for r in rows]

@app.post('/api/admin/user/{uid}/toggle')
def admin_toggle(request:Request,uid:int):
    u=require_user(request)
    if not u['is_admin']: raise HTTPException(403,'Admin only')
    c=db(); c.execute('UPDATE users SET active=1-active WHERE id=?',(uid,)); c.commit(); c.close(); return {'ok':True}

@app.delete('/api/admin/user/{uid}')
def admin_delete(request:Request,uid:int):
    u=require_user(request)
    if not u['is_admin'] or uid==u['id']: raise HTTPException(403,'Not allowed')
    c=db(); c.execute('DELETE FROM users WHERE id=?',(uid,)); c.commit(); c.close(); return {'ok':True}
