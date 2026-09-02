import os
import re
import json
import time
import uuid
import html
import sqlite3
import secrets
import asyncio
import threading
import hashlib
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import bcrypt
import requests
import uvicorn
import edge_tts
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from pydub import AudioSegment
from pydub.generators import Sine, WhiteNoise

APP_NAME = "GS Podcast Studio V6"
BASE = Path(__file__).resolve().parent
DB_DIR = BASE / "instance"
DB_PATH = DB_DIR / "data.db"
STORAGE = BASE / "storage"
OUTPUTS = STORAGE / "outputs"
UPLOADS = STORAGE / "uploads"
for p in (DB_DIR, OUTPUTS, UPLOADS): p.mkdir(parents=True, exist_ok=True)

SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)
FERNET_KEY = base64_key = __import__('base64').urlsafe_b64encode(hashlib.sha256(SESSION_SECRET.encode()).digest())
FERNET = Fernet(FERNET_KEY)

app = FastAPI(title=APP_NAME, version="6.0.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60*60*24, same_site="lax", https_only=True)

LANGUAGES = [
    "English", "Swahili", "Sheng", "Swahili + English", "French", "Spanish", "German", "Portuguese",
    "Arabic", "Hindi", "Chinese", "Japanese", "Korean", "Italian", "Dutch", "Turkish", "Russian",
    "Amharic", "Somali", "Luganda"
]
AGES = ["Child", "Adult", "Old"]
GENDERS = ["Male", "Female"]

VOICE_CATALOG = {
    "English": {"Male":[("Andrew","en-US-AndrewNeural"),("Brian","en-US-BrianNeural"),("Guy","en-US-GuyNeural"),("Ryan","en-GB-RyanNeural")],"Female":[("Jenny","en-US-JennyNeural"),("Aria","en-US-AriaNeural"),("Sonia","en-GB-SoniaNeural")]},
    "Swahili": {"Male":[("Rafiki","sw-KE-RafikiNeural")],"Female":[("Zuri","sw-KE-ZuriNeural")]},
    "French": {"Male":[("Henri","fr-FR-HenriNeural")],"Female":[("Denise","fr-FR-DeniseNeural")]},
    "Spanish": {"Male":[("Alvaro","es-ES-AlvaroNeural")],"Female":[("Elvira","es-ES-ElviraNeural")]},
    "German": {"Male":[("Conrad","de-DE-ConradNeural")],"Female":[("Katja","de-DE-KatjaNeural")]},
    "Portuguese": {"Male":[("Antonio","pt-BR-AntonioNeural")],"Female":[("Francisca","pt-BR-FranciscaNeural")]},
    "Arabic": {"Male":[("Hamed","ar-SA-HamedNeural")],"Female":[("Zariyah","ar-SA-ZariyahNeural")]},
    "Hindi": {"Male":[("Madhur","hi-IN-MadhurNeural")],"Female":[("Swara","hi-IN-SwaraNeural")]},
    "Chinese": {"Male":[("Yunxi","zh-CN-YunxiNeural")],"Female":[("Xiaoxiao","zh-CN-XiaoxiaoNeural")]},
    "Japanese": {"Male":[("Keita","ja-JP-KeitaNeural")],"Female":[("Nanami","ja-JP-NanamiNeural")]},
    "Korean": {"Male":[("InJoon","ko-KR-InJoonNeural")],"Female":[("SunHi","ko-KR-SunHiNeural")]},
    "Italian": {"Male":[("Diego","it-IT-DiegoNeural")],"Female":[("Elsa","it-IT-ElsaNeural")]},
    "Dutch": {"Male":[("Maarten","nl-NL-MaartenNeural")],"Female":[("Colette","nl-NL-ColetteNeural")]},
    "Turkish": {"Male":[("Ahmet","tr-TR-AhmetNeural")],"Female":[("Emel","tr-TR-EmelNeural")]},
    "Russian": {"Male":[("Dmitry","ru-RU-DmitryNeural")],"Female":[("Svetlana","ru-RU-SvetlanaNeural")]},
    "Amharic": {"Male":[("Ameha","am-ET-AmehaNeural")],"Female":[("Mekdes","am-ET-MekdesNeural")]},
    "Somali": {"Male":[("Liban","so-SO-MuuseNeural")],"Female":[("Ubax","so-SO-UbaxNeural")]},
    "Luganda": {"Male":[("Default","sw-KE-RafikiNeural")],"Female":[("Default","sw-KE-ZuriNeural")]},
    "Sheng": {"Male":[("Rafiki","sw-KE-RafikiNeural")],"Female":[("Zuri","sw-KE-ZuriNeural")]},
    "Swahili + English": {"Male":[("Rafiki","sw-KE-RafikiNeural")],"Female":[("Zuri","sw-KE-ZuriNeural")]},
}

PROVIDERS = {
    "Gemini": {"type":"gemini","env":"GEMINI_API_KEY","default_model":"gemini-3.7-flash","models":["gemini-3.7-flash","gemini-3.6-flash","gemini-3.5-flash"]},
    "OpenAI": {"type":"openai","base":"https://api.openai.com/v1/chat/completions","env":"OPENAI_API_KEY","default_model":"gpt-4o-mini"},
    "DeepSeek": {"type":"openai","base":"https://api.deepseek.com/chat/completions","env":"DEEPSEEK_API_KEY","default_model":"deepseek-chat"},
    "Grok": {"type":"openai","base":"https://api.x.ai/v1/chat/completions","env":"XAI_API_KEY","default_model":"grok-3-mini"},
    "Perplexity": {"type":"openai","base":"https://api.perplexity.ai/chat/completions","env":"PERPLEXITY_API_KEY","default_model":"sonar"},
    "Qwen": {"type":"openai","base":"https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions","env":"QWEN_API_KEY","default_model":"qwen-plus"},
    "Z.ai": {"type":"openai","base":"https://api.z.ai/api/paas/v4/chat/completions","env":"ZAI_API_KEY","default_model":"glm-4.5-air"},
    "Groq": {"type":"openai","base":"https://api.groq.com/openai/v1/chat/completions","env":"GROQ_API_KEY","default_model":"llama-3.3-70b-versatile"},
    "OpenRouter": {"type":"openai","base":"https://openrouter.ai/api/v1/chat/completions","env":"OPENROUTER_API_KEY","default_model":"meta-llama/llama-3.3-8b-instruct:free"},
    "Ollama": {"type":"openai","base":"http://localhost:11434/v1/chat/completions","env":"OLLAMA_API_KEY","default_model":"llama3.2"},
    "Custom": {"type":"openai","base":"","env":"","default_model":""},
}

CSS = r'''<style>
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#13213b,#070d18 55%);color:#e5e7eb;font-family:Inter,Arial,sans-serif;min-height:100vh}.container{width:min(1500px,96%);margin:22px auto 70px}.card{background:rgba(13,23,40,.97);border:1px solid #27364d;border-radius:16px;padding:20px;margin-bottom:18px;box-shadow:0 15px 45px rgba(0,0,0,.25)}.brand{color:#60a5fa}.muted{color:#94a3b8}.success{color:#4ade80}.error{color:#f87171}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}nav{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}nav a,.btn{color:#dbeafe;background:#172235;text-decoration:none;padding:9px 13px;border-radius:8px;border:0;display:inline-block;cursor:pointer;font-weight:700}nav a:hover,.btn:hover{background:#243652}.btn-blue{background:#2563eb}.btn-green{background:#15803d}.btn-red{background:#b91c1c}.btn-gray{background:#334155}label{display:block;color:#cbd5e1;margin:0 0 7px;font-weight:600}input,textarea,select{width:100%;background:#07101d;color:#fff;border:1px solid #334155;border-radius:9px;padding:11px;margin-bottom:13px;font-size:14px}textarea{min-height:300px;resize:vertical;line-height:1.6}.speaker{background:#0a1424;border:1px solid #26364e;border-radius:12px;padding:15px}.range{display:flex;gap:10px;align-items:center}.range input{margin:0}.status{background:#08111f;border:1px solid #26364e;border-radius:12px;padding:16px}.progress{height:15px;background:#1e293b;border-radius:999px;overflow:hidden}.bar{height:100%;width:0;background:linear-gradient(90deg,#2563eb,#60a5fa);transition:width .3s}.log{background:#091321;border:1px solid #26364e;border-radius:10px;padding:14px;margin-bottom:10px}.pill{display:inline-block;padding:5px 9px;border-radius:999px;background:#172235;margin:2px}.login{max-width:460px;margin:90px auto}.actions{display:flex;flex-wrap:wrap;gap:8px}.audio{width:100%;margin-top:8px}.danger{border-left:4px solid #ef4444}.small{font-size:12px}@media(max-width:700px){.container{width:97%}.card{padding:15px}}
</style>'''

def db(): return sqlite3.connect(DB_PATH, timeout=30)
def q(sql, params=(), one=False):
    c=db(); cur=c.cursor(); cur.execute(sql, params); rows=cur.fetchone() if one else cur.fetchall(); c.close(); return rows

def execsql(sql, params=()):
    c=db(); cur=c.cursor(); cur.execute(sql, params); c.commit(); rid=cur.lastrowid; c.close(); return rid

def enc(v): return FERNET.encrypt(v.encode()).decode() if v else ""
def dec(v):
    try: return FERNET.decrypt(v.encode()).decode() if v else ""
    except Exception: return ""

def cfg(key, default=""):
    r=q("SELECT value FROM configs WHERE key=?",(key,),True); return r[0] if r else default

def setcfg(key,value): execsql("INSERT OR REPLACE INTO configs(key,value) VALUES(?,?)",(key,value))

def init_db():
    c=db(); cur=c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT DEFAULT 'user',active INTEGER DEFAULT 1,created_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS configs(key TEXT PRIMARY KEY,value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS user_api_keys(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,provider TEXT,key_enc TEXT,base_url TEXT,model TEXT,active INTEGER DEFAULT 1,UNIQUE(user_id,provider))")
    cur.execute("CREATE TABLE IF NOT EXISTS podcasts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,title TEXT,provider TEXT,model TEXT,language TEXT,created_at TEXT,mp3_path TEXT,wav_path TEXT,script TEXT,settings_json TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT,username TEXT,status TEXT,log_text TEXT,saved_file_path TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS uploads(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,kind TEXT,path TEXT,original_name TEXT,created_at TEXT)")
    cur.execute("SELECT id FROM users WHERE username='admin'")
    if not cur.fetchone():
        pw=os.getenv('ADMIN_PASSWORD','smbagathi'); h=bcrypt.hashpw(pw.encode(),bcrypt.gensalt()).decode(); cur.execute("INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",('admin',h,'admin',1,time.strftime('%Y-%m-%d %H:%M:%S')))
    c.commit(); c.close()

@app.on_event('startup')
def startup(): init_db()

def user_row(request):
    u=request.session.get('username'); return q("SELECT id,username,role,active FROM users WHERE username=?",(u,),True) if u else None

def require(request, admin=False):
    u=user_row(request)
    if not u or not u[3]: return RedirectResponse('/login',303)
    if admin and u[2] != 'admin': return HTMLResponse('<h2>Administrator access required.</h2>',403)
    return None

def ai_key(user_id, provider):
    r=q("SELECT key_enc,base_url,model,active FROM user_api_keys WHERE user_id=? AND provider=?",(user_id,provider),True)
    if r and r[3] and r[0]: return dec(r[0]), r[1], r[2]
    p=PROVIDERS.get(provider,{})
    return os.getenv(p.get('env',''), '') or dec(cfg('key_'+provider,'')), cfg('base_'+provider,p.get('base','')), cfg('model_'+provider,p.get('default_model',''))

def prompt_for(text, language, style):
    lang_note={
      'Sheng':'Use natural Kenyan Sheng mixed with Swahili where appropriate; keep it authentic, conversational and easy to speak.',
      'Swahili + English':'Naturally code-switch between Kenyan Swahili and English; do not translate every sentence.',
      'Swahili':'Use natural Kenyan Swahili.',
    }.get(language,f"Write the spoken podcast in {language}.")
    return f'''You are the senior podcast producer for {APP_NAME}. Rewrite the source into a lively, factual, natural spoken podcast. {lang_note}\nStyle: {style}. Preserve names, facts and figures. Do not fabricate quotes or unsupported facts. Create natural transitions, occasional short reactions, and clear speaker-ready paragraphs. Return ONLY the podcast script.\n\nSOURCE:\n{text}'''

def ai_generate(user_id, provider, text, language, style, model_override=''):
    key, base, model=ai_key(user_id,provider); model=model_override or model
    if provider == 'Gemini':
        if not key: raise Exception('Gemini API key is not configured.')
        from google import genai
        client=genai.Client(api_key=key); last=None
        models=[model] if model else PROVIDERS['Gemini']['models']
        for m in models:
            try:
                r=client.models.generate_content(model=m,contents=prompt_for(text,language,style));
                if r and getattr(r,'text',None): return r.text.strip(),m
            except Exception as e: last=e
        raise Exception(f'Gemini generation failed: {last}')
    if not base: raise Exception(f'{provider} base URL is not configured.')
    if not key and provider not in ('Ollama','Custom'): raise Exception(f'{provider} API key is not configured.')
    headers={'Content-Type':'application/json'}
    if key: headers['Authorization']='Bearer '+key
    if provider=='OpenRouter': headers.update({'HTTP-Referer':cfg('public_url',''),'X-Title':APP_NAME})
    payload={'model':model or 'default','messages':[{'role':'system','content':'You are a professional podcast producer.'},{'role':'user','content':prompt_for(text,language,style)}],'temperature':0.8}
    r=requests.post(base,headers=headers,json=payload,timeout=180); r.raise_for_status(); data=r.json(); return data['choices'][0]['message']['content'].strip(),model

def voice_for(language,gender,name):
    d=VOICE_CATALOG.get(language,VOICE_CATALOG['English']); arr=d.get(gender,d['Male']);
    for n,v in arr:
        if n==name:return v
    return arr[0][1]

async def tts(text,voice,rate,path):
    await edge_tts.Communicate(text=text,voice=voice,rate=rate).save(path)

def ffmpeg_pitch(src,dst,semitones):
    if abs(semitones)<0.01:
        AudioSegment.from_file(src).export(dst,format='mp3'); return
    factor=2**(semitones/12); rate=max(0.5,min(2.0,factor));
    cmd=['ffmpeg','-y','-i',src,'-filter:a',f"asetrate=44100*{factor},aresample=44100,atempo={1/rate}",dst]
    subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)

def age_shift(age): return {'Child':4,'Adult':0,'Old':-3}.get(age,0)
def speed_rate(speed): return f"{int((float(speed)-1)*100):+d}%"

def synthetic_crowd(kind,duration):
    dur=max(1200,min(duration,9000)); noise=WhiteNoise().to_audio_segment(duration=dur).apply_gain(-30)
    base=Sine(170).to_audio_segment(duration=dur).apply_gain(-34)
    if kind=='cheering':
        out=noise.overlay(base); 
        for x in range(0,dur,650): out=out.overlay(Sine(700).to_audio_segment(duration=140).apply_gain(-16),position=x)
        return out.fade_in(150).fade_out(400)
    if kind=='questions': return noise.overlay(Sine(320).to_audio_segment(duration=dur).apply_gain(-25)).fade_in(200).fade_out(300)
    if kind=='comments': return noise.overlay(Sine(230).to_audio_segment(duration=dur).apply_gain(-26)).fade_in(150).fade_out(250)
    return noise.overlay(Sine(260).to_audio_segment(duration=dur).apply_gain(-24)).fade_in(180).fade_out(300)

def mix_track(base, path, volume_db, loop=True):
    if not path or not os.path.isfile(path): return base
    bg=AudioSegment.from_file(path)
    if loop and len(bg)<len(base): bg=(bg*((len(base)//len(bg))+1))[:len(base)]
    else: bg=bg[:len(base)]
    return base.overlay(bg.apply_gain(float(volume_db)))

def build_audio(user_id, speakers, intro, outro, script, speed, music_path, music_volume, crowd_kind, crowd_volume, extra_path, extra_volume):
    work=OUTPUTS/f'job_{uuid.uuid4().hex}'; work.mkdir(parents=True,exist_ok=True)
    combined=AudioSegment.empty(); paras=[p.strip() for p in re.split(r'\n\s*\n',script) if p.strip()]
    sections=[]
    if intro.strip(): sections.append(('intro',intro,speakers[0]))
    for i,p in enumerate(paras): sections.append((f'section_{i}',p,speakers[i%len(speakers)]))
    if outro.strip(): sections.append(('outro',outro,speakers[-1]))
    for i,(name,text,s) in enumerate(sections):
        raw=work/f'{name}.mp3'; shifted=work/f'{name}_shift.mp3'; voice=voice_for(s.get('language','English'),s.get('gender','Male'),s.get('voice',''))
        asyncio.run(tts(text,voice,speed_rate(speed),str(raw))); ffmpeg_pitch(str(raw),str(shifted),age_shift(s.get('age','Adult')))
        a=AudioSegment.from_file(shifted); combined += a + AudioSegment.silent(duration=240)
    combined=mix_track(combined,music_path,music_volume)
    if crowd_kind and crowd_kind!='off': combined=combined.overlay(synthetic_crowd(crowd_kind,min(6500,len(combined))),position=max(0,min(len(combined)-6500,len(combined)//3))).apply_gain(0)
    combined=mix_track(combined,extra_path,extra_volume,False)
    stamp=int(time.time()); mp3=OUTPUTS/f'podcast_{stamp}_{user_id}.mp3'; wav=OUTPUTS/f'podcast_{stamp}_{user_id}.wav'
    combined.export(mp3,format='mp3',bitrate='160k'); combined.export(wav,format='wav')
    return str(mp3),str(wav)

def page(title,body,username=''):
    nav=f'''<div class="card"><h1 class="brand">🎙 {APP_NAME}</h1><div class="muted">{html.escape(username)}</div><nav><a href="/">Workspace</a><a href="/podcasts">My Podcasts</a><a href="/my-apis">My AI APIs</a><a href="/configuration">Configuration</a><a href="/users">Users</a><a href="/logs">Logs</a><a href="/logout">Logout</a></nav></div>'''
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>{CSS}</head><body><div class="container">{nav}{body}</div></body></html>')

@app.get('/login',response_class=HTMLResponse)
def login_page(request:Request):
    if user_row(request): return RedirectResponse('/',303)
    return HTMLResponse(f'''<!doctype html><html><head>{CSS}</head><body><div class="container"><div class="card login"><h1 class="brand">🎙 {APP_NAME}</h1><form method="post"><label>Username</label><input name="username" required><label>Password</label><input name="password" type="password" required><button class="btn btn-blue">Login</button></form></div></div></body></html>''')

@app.post('/login')
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    r=q('SELECT password_hash,active FROM users WHERE username=?',(username.strip(),),True)
    if r and r[1] and bcrypt.checkpw(password.encode(),r[0].encode()): request.session['username']=username.strip(); return RedirectResponse('/',303)
    return HTMLResponse(f'{CSS}<div class="container"><div class="card login"><h2 class="error">Invalid login or disabled account.</h2><a class="btn" href="/login">Try again</a></div></div>',401)

@app.get('/logout')
def logout(request:Request): request.session.clear(); return RedirectResponse('/login',303)

@app.get('/',response_class=HTMLResponse)
def workspace(request:Request):
    red=require(request)
    if red:return red
    username=user_row(request)[1]
    langs=''.join(f'<option>{html.escape(x)}</option>' for x in LANGUAGES)
    providers=''.join(f'<option>{html.escape(x)}</option>' for x in PROVIDERS)
    voices_json=json.dumps({k:{g:[n for n,v in vals] for g,vals in d.items()} for k,d in VOICE_CATALOG.items()})
    body=f'''<div class="card"><h2>🚀 Podcast Workspace</h2><div class="grid"><div><label>AI engine</label><select id="provider">{providers}</select></div><div><label>Model (optional override)</label><input id="model" placeholder="Uses configured default"></div><div><label>Podcast language</label><select id="language">{langs}</select></div><div><label>Style</label><select id="style"><option>Lively broadcast</option><option>News</option><option>Educational</option><option>Interview</option><option>Storytelling</option><option>Comedy/light</option><option>Serious documentary</option></select></div></div><label>Topic / Script / Article / Notes</label><textarea id="article" placeholder="Paste your source here..."></textarea><div class="actions"><button class="btn btn-gray" onclick="clearText()">🗑 Clear</button><button class="btn btn-gray" onclick="sample()">📄 Sample</button><button class="btn btn-blue" onclick="rewrite()">✨ Rewrite with selected AI</button></div><p id="aiStatus" class="muted"></p></div>
<div class="card"><h2>🎙 Speakers</h2><label>Number of speakers</label><select id="speakerCount" onchange="renderSpeakers()"><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option></select><div id="speakers" class="grid"></div></div>
<div class="card"><h2>🎬 Podcast Structure</h2><div class="grid"><div><label>Intro</label><textarea id="intro" style="min-height:130px"></textarea></div><div><label>Outro</label><textarea id="outro" style="min-height:130px"></textarea></div></div></div>
<div class="card"><h2>🎚 Audio & Production</h2><div class="grid"><div><label>Speech speed</label><input id="speed" type="range" min="0.70" max="1.50" step="0.05" value="1" oninput="speedVal.textContent=this.value+'x'"><span id="speedVal">1x</span></div><div><label>Background music / ambience</label><input id="music" type="file" accept="audio/*"><select id="musicAction"><option value="use">Use uploaded music</option><option value="remove">Remove background music</option></select></div><div><label>Music volume</label><input id="musicVolume" type="range" min="-30" max="0" value="-12" oninput="musicVal.textContent=this.value+' dB'"><span id="musicVal">-12 dB</span></div><div><label>Merge extra audio</label><input id="extra" type="file" accept="audio/*"><input id="extraVolume" type="range" min="-30" max="0" value="-10"></div><div><label>Audience atmosphere</label><select id="crowd"><option value="off">Off</option><option value="cheering">👏 Cheering / applause</option><option value="questions">🎤 Audience asking questions</option><option value="comments">💬 Audience commenting</option><option value="contributions">🙋 Audience contributions</option></select><input id="crowdVolume" type="range" min="-30" max="0" value="-18"></div></div><p class="muted small">Sheng and Swahili+English are script modes; TTS uses the closest supported Kenyan Swahili voice so code-switching remains natural.</p></div>
<div class="card"><button id="go" class="btn btn-green" onclick="processPodcast()">⚡ Generate Podcast</button><div class="status"><b id="status">Waiting for production...</b><div class="progress"><div id="bar" class="bar"></div></div><div id="result"></div></div></div>
<script>const VOICES={voices_json};function renderSpeakers(){{let n=+speakerCount.value,c=speakers;c.innerHTML='';for(let i=1;i<=n;i++){{let d=document.createElement('div');d.className='speaker';d.innerHTML=`<h3>Speaker ${{i}}</h3><label>Name</label><input class="sp-name" value="Speaker ${{i}}"><label>Gender</label><select class="sp-gender" id="g${{i}}" onchange="refreshVoice(${{i}})"><option>Male</option><option>Female</option></select><label>Age</label><select class="sp-age"><option>Child</option><option selected>Adult</option><option>Old</option></select><label>Language</label><select class="sp-lang" id="l${{i}}" onchange="refreshVoice(${{i}})">{langs}</select><label>Voice</label><select class="sp-voice" id="v${{i}}"></select>`;c.appendChild(d);refreshVoice(i)}}}}function refreshVoice(i){{let l=document.getElementById('l'+i).value,g=document.getElementById('g'+i).value,s=document.getElementById('v'+i);s.innerHTML='';(VOICES[l]&&VOICES[l][g]?VOICES[l][g]:['Default']).forEach(v=>{{let o=document.createElement('option');o.textContent=v;o.value=v;s.appendChild(o)}})}}function collect(){{return [...document.querySelectorAll('.speaker')].map((x,i)=>({{name:x.querySelector('.sp-name').value,gender:x.querySelector('.sp-gender').value,age:x.querySelector('.sp-age').value,language:x.querySelector('.sp-lang').value,voice:x.querySelector('.sp-voice').value}}))}}function clearText(){{article.value='';aiStatus.textContent='Editor cleared.'}}function sample(){{article.value='Welcome to our podcast. Today we explore how AI is changing media, business and everyday life in Kenya. We will hear different perspectives, practical examples and a few audience reactions.'}}async function rewrite(){{if(!article.value.trim())return alert('Enter source text first.');aiStatus.textContent='Generating with '+provider.value+'...';let r=await fetch('/rewrite',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text:article.value,provider:provider.value,model:model.value,language:language.value,style:style.value}})}});let d=await r.json();if(!r.ok){{aiStatus.textContent=d.detail||'AI error';return}}article.value=d.text;aiStatus.textContent='Completed with '+d.model}}async function uploadOne(id,kind){{let f=document.getElementById(id).files[0];if(!f)return '';let fd=new FormData();fd.append('file',f);fd.append('kind',kind);let r=await fetch('/upload-audio',{{method:'POST',body:fd}});let d=await r.json();return d.path}}async function processPodcast(){{if(!article.value.trim())return alert('Enter source text.');go.disabled=true;result.innerHTML='';status.textContent='Uploading media...';let music=musicAction.value==='remove'?'':await uploadOne('music','music');let extra=await uploadOne('extra','merge');let payload={{text:article.value,provider:provider.value,model:model.value,language:language.value,style:style.value,intro:intro.value,outro:outro.value,speed:+speed.value,music_path:music,music_volume:+musicVolume.value,crowd:crowd.value,crowd_volume:+crowdVolume.value,extra_path:extra,extra_volume:+extraVolume.value,speakers:collect()}};let r=await fetch('/process',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});let d=await r.json();if(!r.ok){{status.textContent=d.detail||'Could not start';go.disabled=false;return}}poll()}}async function poll(){{let r=await fetch('/job-status'),d=await r.json();status.textContent=d.status;bar.style.width=(d.progress*100)+'%';if(d.running)return setTimeout(poll,1200);go.disabled=false;if(d.error)return status.innerHTML='<span class="error">'+d.error+'</span>';if(d.mp3)result.innerHTML=`<audio class="audio" controls src="${{d.mp3}}"></audio><div class="actions"><a class="btn btn-green" href="${{d.mp3}}">⬇ MP3</a><a class="btn btn-green" href="${{d.wav}}">⬇ WAV</a><a class="btn" target="_blank" href="https://wa.me/?text=${{encodeURIComponent('Listen to my podcast: '+location.origin+d.mp3)}}">WhatsApp</a><a class="btn" href="mailto:?subject=Podcast&body=${{encodeURIComponent('Listen to my podcast: '+location.origin+d.mp3)}}">Email</a></div>`}}renderSpeakers();</script></div>'''
    return page('Workspace',body,username)

@app.post('/rewrite')
async def rewrite(request:Request):
    red=require(request)
    if red:return red
    p=await request.json(); u=user_row(request); text=str(p.get('text','')).strip()
    if not text:return JSONResponse({'detail':'Text cannot be empty.'},400)
    try:
        out,model=ai_generate(u[0],p.get('provider','Gemini'),text,p.get('language','English'),p.get('style','Lively broadcast'),p.get('model','')); return {'text':out,'model':model}
    except Exception as e:return JSONResponse({'detail':str(e)},503)

job_lock=threading.Lock(); job={'running':False,'progress':0,'status':'Waiting','mp3':'','wav':'','error':''}
@app.post('/upload-audio')
async def upload_audio(request:Request,file:UploadFile=File(...),kind:str=Form('audio')):
    red=require(request)
    if red:return red
    ext=Path(file.filename or '').suffix.lower() or '.mp3'; allowed={'.mp3','.wav','.m4a','.ogg','.aac','.flac'}
    if ext not in allowed:return JSONResponse({'detail':'Unsupported audio format.'},400)
    name=f'{uuid.uuid4().hex}{ext}'; path=UPLOADS/name; path.write_bytes(await file.read()); uid=user_row(request)[0]; execsql('INSERT INTO uploads(user_id,kind,path,original_name,created_at) VALUES(?,?,?,?,?)',(uid,kind,str(path),file.filename,time.strftime('%Y-%m-%d %H:%M:%S'))); return {'path':str(path),'name':file.filename}

@app.post('/process')
async def process(request:Request):
    red=require(request)
    if red:return red
    u=user_row(request); p=await request.json(); text=str(p.get('text','')).strip()
    if not text:return JSONResponse({'detail':'Podcast text cannot be empty.'},400)
    with job_lock:
        if job['running']:return JSONResponse({'detail':'Another podcast job is already running.'},409)
        job.update(running=True,progress=.02,status='Preparing production...',mp3='',wav='',error='')
    def worker():
        try:
            setjob('AI is preparing the podcast script...',.10); script,model=ai_generate(u[0],p.get('provider','Gemini'),text,p.get('language','English'),p.get('style','Lively broadcast'),p.get('model',''))
            setjob('Building voices and speaker performance...',.32)
            speakers=p.get('speakers') or [{'name':'Speaker 1','gender':'Male','age':'Adult','language':p.get('language','English'),'voice':'Andrew'}]
            mp3,wav=build_audio(u[0],speakers,str(p.get('intro','')),str(p.get('outro','')),script,float(p.get('speed',1)),p.get('music_path',''),float(p.get('music_volume',-12)),p.get('crowd','off'),float(p.get('crowd_volume',-18)),p.get('extra_path',''),float(p.get('extra_volume',-10)))
            setjob('Podcast completed successfully.',1); job['mp3']='/download/'+Path(mp3).name; job['wav']='/download/'+Path(wav).name
            title=(script.split('\n')[0][:90] or 'Untitled Podcast'); execsql('INSERT INTO podcasts(user_id,title,provider,model,language,created_at,mp3_path,wav_path,script,settings_json) VALUES(?,?,?,?,?,?,?,?,?,?)',(u[0],title,p.get('provider','Gemini'),model,p.get('language','English'),time.strftime('%Y-%m-%d %H:%M:%S'),mp3,wav,script,json.dumps(p)))
            execsql('INSERT INTO logs(timestamp,username,status,log_text,saved_file_path) VALUES(?,?,?,?,?)',(time.strftime('%Y-%m-%d %H:%M:%S'),u[1],'COMPLETED',f'Podcast generated with {p.get("provider","Gemini")} / {model}',mp3))
        except Exception as e:
            job.update(running=False,progress=0,error=str(e),status='Pipeline error: '+str(e)); execsql('INSERT INTO logs(timestamp,username,status,log_text,saved_file_path) VALUES(?,?,?,?,?)',(time.strftime('%Y-%m-%d %H:%M:%S'),u[1],'FAILED',str(e),'')); return
        job['running']=False
    threading.Thread(target=worker,daemon=True).start(); return {'started':True}

def setjob(status,progress):
    with job_lock: job['status']=status; job['progress']=progress

@app.get('/job-status')
def job_status(request:Request):
    red=require(request)
    if red:return red
    with job_lock:return dict(job)

@app.get('/download/{filename}')
def download(request:Request,filename:str):
    red=require(request)
    if red:return red
    path=OUTPUTS/Path(filename).name
    if not path.is_file():return HTMLResponse('File not found.',404)
    return FileResponse(path,media_type='audio/wav' if path.suffix=='.wav' else 'audio/mpeg',filename=path.name)

@app.get('/podcasts',response_class=HTMLResponse)
def podcasts(request:Request):
    red=require(request)
    if red:return red
    u=user_row(request)
    if u[2]=='admin':
        rows=q('SELECT p.id,p.title,p.provider,p.model,p.language,p.created_at,p.mp3_path,p.wav_path,p.user_id,u.username FROM podcasts p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.id DESC')
    else:
        rows=q('SELECT p.id,p.title,p.provider,p.model,p.language,p.created_at,p.mp3_path,p.wav_path,p.user_id,u.username FROM podcasts p LEFT JOIN users u ON u.id=p.user_id WHERE p.user_id=? ORDER BY p.id DESC',(u[0],))
    cards=''
    for r in rows:
        fn=Path(r[6]).name
        wf=Path(r[7]).name
        share=quote('Listen: '+cfg('public_url','')+'/download/'+fn)
        cards += f'<div class="log"><b>{html.escape(r[1])}</b><div class="muted">{r[5]} · {r[2]} · {r[4]} · Creator: {html.escape(r[9] or "")}</div><audio class="audio" controls src="/download/{html.escape(fn)}"></audio><div class="actions"><a class="btn btn-green" href="/download/{html.escape(fn)}">MP3</a><a class="btn btn-green" href="/download/{html.escape(wf)}">WAV</a><a class="btn" target="_blank" href="https://wa.me/?text={share}">WhatsApp</a><a class="btn" href="mailto:?subject=Podcast&body={share}">Email</a></div></div>'
    heading='🎛 All Podcasts (Admin)' if u[2]=='admin' else '🎧 My Podcasts'
    return page('Podcasts',f'<div class="card"><h2>{heading}</h2>{cards or "<p class=muted>No podcasts yet.</p>"}</div>',u[1])

@app.get('/my-apis',response_class=HTMLResponse)
def myapis(request:Request):
    red=require(request)
    if red:return red
    u=user_row(request); rows=q('SELECT provider,base_url,model,active FROM user_api_keys WHERE user_id=? ORDER BY provider',(u[0],)); providers=''.join(f'<option>{x}</option>' for x in PROVIDERS)
    items=''.join(f'<div class="log"><b>{html.escape(r[0])}</b> <span class="pill">{"ACTIVE" if r[3] else "DISABLED"}</span><div class="muted">{html.escape(r[1] or "default")} · {html.escape(r[2] or "default")}</div></div>' for r in rows)
    body=f'''<div class="card"><h2>🔑 My AI API Connections</h2><p class="muted">Your keys are encrypted at rest and can override administrator defaults for your own workspace.</p><form method="post"><label>Provider</label><select name="provider">{providers}</select><label>API key</label><input type="password" name="api_key" autocomplete="off"><label>Base URL (optional for custom/OpenAI-compatible APIs)</label><input name="base_url"><label>Model</label><input name="model"><button class="btn btn-green">Save / Update</button></form></div><div class="card"><h2>Saved connections</h2>{items or '<p class="muted">None.</p>'}</div>'''
    return page('My AI APIs',body,u[1])

@app.post('/my-apis')
def save_my_api(request:Request,provider:str=Form(...),api_key:str=Form(''),base_url:str=Form(''),model:str=Form('')):
    red=require(request)
    if red:return red
    u=user_row(request); p=PROVIDERS.get(provider,{}); base=base_url.strip() or p.get('base',''); mdl=model.strip() or p.get('default_model',''); execsql('INSERT OR REPLACE INTO user_api_keys(user_id,provider,key_enc,base_url,model,active) VALUES(?,?,?,?,?,1)',(u[0],provider,enc(api_key.strip()),base,mdl)); return RedirectResponse('/my-apis',303)

@app.get('/configuration',response_class=HTMLResponse)
def configuration(request:Request):
    red=require(request,True)
    if red:return red
    rows=[]
    for name,p in PROVIDERS.items():
        has=bool(os.getenv(p.get('env','')) or cfg('key_'+name,'')); rows.append(f'<div class="log"><b>{html.escape(name)}</b><span class="pill">{"KEY CONFIGURED" if has else "NO SYSTEM KEY"}</span><div class="muted">Model: {html.escape(cfg("model_"+name,p.get("default_model","")))}<br>Base: {html.escape(cfg("base_"+name,p.get("base","")))}</div></div>')
    body=f'''<div class="card"><h2>⚙️ AI / Database / System Configuration</h2><p class="muted">Configure administrator defaults. Users may override them from My AI APIs.</p><form method="post"><label>Public URL (used by share links)</label><input name="public_url" value="{html.escape(cfg('public_url',''))}" placeholder="https://your-service.onrender.com"><label>System provider</label><select name="provider">{''.join(f'<option>{x}</option>' for x in PROVIDERS)}</select><label>System API key</label><input type="password" name="api_key"><label>Model</label><input name="model"><label>OpenAI-compatible Base URL (optional)</label><input name="base_url"><button class="btn btn-green">Save Provider</button></form></div><div class="card"><h2>Configured AI Providers</h2>{''.join(rows)}</div><div class="card"><h2>Database & Runtime</h2><div class="grid"><div class="log"><b>Database</b><br>SQLite: {html.escape(str(DB_PATH))}<br>Users: {q('SELECT COUNT(*) FROM users',(),True)[0]}<br>Podcasts: {q('SELECT COUNT(*) FROM podcasts',(),True)[0]}</div><div class="log"><b>Storage</b><br>Outputs: {html.escape(str(OUTPUTS))}<br>Uploads: {html.escape(str(UPLOADS))}<br>FFmpeg required: yes</div><div class="log"><b>Environment</b><br>Python: {html.escape(os.sys.version.split()[0])}<br>Active AI defaults: {sum(bool(os.getenv(p.get('env','')) or cfg('key_'+n,'')) for n,p in PROVIDERS.items())}/{len(PROVIDERS)}</div></div></div>'''
    return page('Configuration',body,user_row(request)[1])

@app.post('/configuration')
def save_configuration(request:Request,public_url:str=Form(''),provider:str=Form(...),api_key:str=Form(''),model:str=Form(''),base_url:str=Form('')):
    red=require(request,True)
    if red:return red
    setcfg('public_url',public_url.strip()); setcfg('key_'+provider,enc(api_key.strip())); setcfg('model_'+provider,model.strip()); setcfg('base_'+provider,base_url.strip()); return RedirectResponse('/configuration',303)

@app.get('/users',response_class=HTMLResponse)
def users(request:Request):
    red=require(request,True)
    if red:return red
    rows=q('SELECT id,username,role,active,created_at FROM users ORDER BY username')
    items=''
    for r in rows:
        sel_user='selected' if r[2]=='user' else ''
        sel_admin='selected' if r[2]=='admin' else ''
        items += f'''<div class="log"><b>{html.escape(r[1])}</b> <span class="pill">{html.escape(r[2])}</span> <span class="pill">{"ACTIVE" if r[3] else "DISABLED"}</span><form method="post" action="/users/{r[0]}/update"><input name="username" value="{html.escape(r[1])}" required><input name="password" type="password" placeholder="New password (optional)"><select name="role"><option value="user" {sel_user}>user</option><option value="admin" {sel_admin}>admin</option></select><button class="btn">Update</button></form><div class="actions"><form method="post" action="/users/{r[0]}/toggle"><button class="btn">{"Disable" if r[3] else "Activate"}</button></form><form method="post" action="/users/{r[0]}/delete" onsubmit="return confirm('Delete user?')"><button class="btn btn-red">Delete</button></form></div></div>'''
    body=f'''<div class="grid"><div class="card"><h2>➕ Add User</h2><form method="post" action="/users"><label>Username</label><input name="username" required><label>Password</label><input type="password" name="password" required><label>Role</label><select name="role"><option>user</option><option>admin</option></select><button class="btn btn-green">Create</button></form></div><div class="card"><h2>👥 Users</h2>{items}</div></div>'''
    return page('Users',body,user_row(request)[1])

@app.post('/users')
def create_user(request:Request,username:str=Form(...),password:str=Form(...),role:str=Form('user')):
    red=require(request,True)
    if red:return red
    try: execsql('INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)',(username.strip(),bcrypt.hashpw(password.encode(),bcrypt.gensalt()).decode(),role,1,time.strftime('%Y-%m-%d %H:%M:%S')))
    except sqlite3.IntegrityError:return HTMLResponse(f'{CSS}<div class="container"><div class="card error">Username already exists.</div></div>',409)
    return RedirectResponse('/users',303)

@app.post('/users/{uid}/update')
def update_user(request:Request,uid:int,username:str=Form(...),password:str=Form(''),role:str=Form('user')):
    red=require(request,True)
    if red:return red
    if not q('SELECT id FROM users WHERE id=?',(uid,),True): return RedirectResponse('/users',303)
    try:
        if password.strip():
            execsql('UPDATE users SET username=?,password_hash=?,role=? WHERE id=?',(username.strip(),bcrypt.hashpw(password.encode(),bcrypt.gensalt()).decode(),role,uid))
        else:
            execsql('UPDATE users SET username=?,role=? WHERE id=?',(username.strip(),role,uid))
    except sqlite3.IntegrityError:
        return HTMLResponse(f'{CSS}<div class="container"><div class="card error">Username already exists.</div></div>',409)
    return RedirectResponse('/users',303)

@app.post('/users/{uid}/toggle')
def toggle_user(request:Request,uid:int):
    red=require(request,True)
    if red:return red
    if q('SELECT username FROM users WHERE id=?',(uid,),True): execsql('UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(uid,))
    return RedirectResponse('/users',303)

@app.post('/users/{uid}/delete')
def delete_user(request:Request,uid:int):
    red=require(request,True)
    if red:return red
    r=q('SELECT username FROM users WHERE id=?',(uid,),True)
    if r and r[0]!='admin': execsql('DELETE FROM user_api_keys WHERE user_id=?',(uid,)); execsql('DELETE FROM users WHERE id=?',(uid,))
    return RedirectResponse('/users',303)

@app.get('/logs',response_class=HTMLResponse)
def logs(request:Request):
    red=require(request,True)
    if red:return red
    rows=q('SELECT timestamp,username,status,log_text,saved_file_path FROM logs ORDER BY id DESC LIMIT 300'); items=''.join(f'<div class="log"><b>{html.escape(str(r[0]))}</b> · {html.escape(str(r[1]))} · <span class="{"success" if r[2]=="COMPLETED" else "error"}">{html.escape(str(r[2]))}</span><p>{html.escape(str(r[3]))}</p></div>' for r in rows)
    return page('Logs',f'<div class="card"><h2>Execution Logs</h2>{items or "<p class=muted>No logs.</p>"}</div>',user_row(request)[1])

if __name__=='__main__': uvicorn.run('main:app',host='0.0.0.0',port=int(os.getenv('PORT','8080')))
