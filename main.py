import os
import time
import sqlite3
import threading
import html
import secrets
import asyncio
import re
from typing import Optional

import bcrypt
import uvicorn

from fastapi import (
    FastAPI,
    Form,
    Request,
    UploadFile,
    File,
)

from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    JSONResponse,
)

from starlette.middleware.sessions import SessionMiddleware

from pydub import AudioSegment
from pydub.generators import Sine

import edge_tts

from google import genai


# ============================================================
# APPLICATION
# ============================================================

APP_NAME = "gs_podcast_automation"

DB_DIR = "instance"
DB_PATH = os.path.join(DB_DIR, "data.db")

STORAGE_DIR = os.path.join("storage", "outputs")

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)

app = FastAPI(
    title=APP_NAME,
    version="3.0.0",
)

SESSION_SECRET = os.getenv(
    "SESSION_SECRET",
    secrets.token_urlsafe(32)
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=True,
)


# ============================================================
# GEMINI
# ============================================================

# Current Gemini 3 Flash family.
# We don't depend on only one model because 503 capacity
# errors can temporarily affect individual models.

GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

GEMINI_RETRIES = 3


# ============================================================
# DATABASE
# ============================================================

def get_db():

    return sqlite3.connect(
        DB_PATH,
        timeout=30
    )


def database_provisioner():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS configs (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            username TEXT,
            status TEXT,
            log_text TEXT,
            saved_file_path TEXT
        )
    """)

    cursor.execute(
        "SELECT id FROM users WHERE username = ?",
        ("admin",)
    )

    if not cursor.fetchone():

        password = os.getenv(
            "ADMIN_PASSWORD",
            "smbagathi"
        )

        hashed = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt()
        )

        cursor.execute(
            """
            INSERT INTO users
            (username, password_hash)
            VALUES (?, ?)
            """,
            (
                "admin",
                hashed.decode("utf-8")
            )
        )

    conn.commit()
    conn.close()


def pull_config(
    key: str,
    default_val: str = ""
):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT value
        FROM configs
        WHERE key = ?
        """,
        (key,)
    )

    row = cursor.fetchone()

    conn.close()

    return row[0] if row else default_val


def push_config(
    key: str,
    value: str
):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO configs
        (key, value)
        VALUES (?, ?)
        """,
        (
            key,
            value
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# JOB STATE
# ============================================================

job_lock = threading.Lock()

job_state = {
    "running": False,
    "progress": 0,
    "status": "Pipeline awaiting input.",
    "file": "",
    "mp3": "",
    "wav": "",
    "error": "",
}


def set_job(
    status,
    progress
):

    with job_lock:

        job_state["status"] = status
        job_state["progress"] = progress


# ============================================================
# GEMINI ENGINE
# ============================================================

class GeminiEngine:

    def __init__(self):

        self.api_key = (
            pull_config("gemini_api_key")
            or os.getenv(
                "GEMINI_API_KEY",
                ""
            )
        )

        if not self.api_key:

            raise Exception(
                "Gemini API key is not configured."
            )

        self.client = genai.Client(
            api_key=self.api_key
        )

    def rewrite(
        self,
        source_text: str
    ):

        prompt = f"""
You are the senior editorial producer for
{APP_NAME}.

Rewrite the supplied source into a natural,
professional podcast script.

Requirements:

- Preserve the author's meaning.
- Preserve important names, facts and figures.
- Do not invent information.
- Do not fabricate quotes.
- Make it sound natural when spoken.
- Remove newspaper formatting.
- Remove unnecessary headings.
- Use smooth spoken transitions.
- Keep the central argument intact.
- Do not add unsupported opinions.
- Return ONLY the finished podcast script.

SOURCE:

{source_text}
"""

        last_error = None

        for model in GEMINI_MODELS:

            for attempt in range(
                GEMINI_RETRIES
            ):

                try:

                    response = (
                        self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                        )
                    )

                    text = (
                        response.text
                        if response
                        else ""
                    )

                    if text and text.strip():

                        return text.strip(), model

                    last_error = Exception(
                        f"{model} returned empty output."
                    )

                except Exception as error:

                    last_error = error

                    error_text = str(
                        error
                    ).lower()

                    retryable = any(
                        word in error_text
                        for word in [
                            "503",
                            "unavailable",
                            "429",
                            "resource exhausted",
                            "deadline",
                            "timeout",
                            "temporarily",
                        ]
                    )

                    if retryable:

                        time.sleep(
                            2 ** attempt
                        )

                        continue

                    # A model-specific 404 or unsupported
                    # model should immediately move to the
                    # next current model.

                    break

        raise Exception(
            "Gemini generation failed after trying "
            "all configured models. Last error: "
            + str(last_error)
        )


# ============================================================
# EDGE TTS VOICES
# ============================================================

VOICE_CATALOG = {

    "English": {
        "Male": [
            (
                "Andrew",
                "en-US-AndrewNeural"
            ),
            (
                "Brian",
                "en-US-BrianNeural"
            ),
            (
                "Guy",
                "en-US-GuyNeural"
            ),
            (
                "Ryan",
                "en-GB-RyanNeural"
            ),
        ],

        "Female": [
            (
                "Jenny",
                "en-US-JennyNeural"
            ),
            (
                "Aria",
                "en-US-AriaNeural"
            ),
            (
                "Sonia",
                "en-GB-SoniaNeural"
            ),
        ],
    },

    "Swahili": {
        "Male": [
            (
                "Rafiki",
                "sw-KE-RafikiNeural"
            ),
        ],

        "Female": [
            (
                "Zuri",
                "sw-KE-ZuriNeural"
            ),
        ],
    },

    "French": {
        "Male": [
            (
                "Henri",
                "fr-FR-HenriNeural"
            ),
        ],

        "Female": [
            (
                "Denise",
                "fr-FR-DeniseNeural"
            ),
        ],
    },

    "Spanish": {
        "Male": [
            (
                "Alvaro",
                "es-ES-AlvaroNeural"
            ),
        ],

        "Female": [
            (
                "Elvira",
                "es-ES-ElviraNeural"
            ),
        ],
    },
}


def get_voice(
    language,
    gender,
    requested
):

    language_data = VOICE_CATALOG.get(
        language,
        VOICE_CATALOG["English"]
    )

    voices = language_data.get(
        gender,
        language_data["Male"]
    )

    for name, voice in voices:

        if name == requested:

            return voice

    return voices[0][1]


# ============================================================
# AUDIO HELPERS
# ============================================================

async def create_tts(
    text,
    voice,
    rate,
    output_path
):

    communicator = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
    )

    await communicator.save(
        output_path
    )


def speech_rate(
    speed
):

    try:

        speed = float(speed)

    except:

        speed = 1.0

    speed = max(
        0.5,
        min(
            speed,
            2.0
        )
    )

    percentage = int(
        (speed - 1.0) * 100
    )

    if percentage >= 0:

        return f"+{percentage}%"

    return f"{percentage}%"


def make_ambience(
    duration_ms,
    volume
):

    # Soft synthesized ambient bed.
    # No external copyrighted music is required.

    base = Sine(110).to_audio_segment(
        duration=duration_ms
    )

    harmonic = Sine(165).to_audio_segment(
        duration=duration_ms
    )

    base = base.apply_gain(
        -42 + float(volume)
    )

    harmonic = harmonic.apply_gain(
        -48 + float(volume)
    )

    ambience = base.overlay(
        harmonic
    )

    return ambience


def apply_ambience(
    speech,
    volume
):

    ambience = make_ambience(
        len(speech),
        float(volume)
    )

    return ambience.overlay(
        speech
    )


# ============================================================
# PODCAST AUDIO ENGINE
# ============================================================

class PodcastAudioEngine:

    def __init__(
        self,
        speakers,
        intro,
        outro,
        ambience_enabled,
        ambience_volume,
        speed
    ):

        self.speakers = speakers
        self.intro = intro.strip()
        self.outro = outro.strip()
        self.ambience_enabled = ambience_enabled
        self.ambience_volume = ambience_volume
        self.speed = speed

    def build(
        self,
        script
    ):

        timestamp = int(
            time.time()
        )

        working_dir = os.path.join(
            STORAGE_DIR,
            f"job_{timestamp}"
        )

        os.makedirs(
            working_dir,
            exist_ok=True
        )

        final_mp3 = os.path.join(
            STORAGE_DIR,
            f"podcast_{timestamp}.mp3"
        )

        final_wav = os.path.join(
            STORAGE_DIR,
            f"podcast_{timestamp}.wav"
        )

        # ----------------------------------------------------
        # INTRO
        # ----------------------------------------------------

        sections = []

        if self.intro:

            sections.append(
                (
                    "intro",
                    self.intro,
                    self.speakers[0]
                )
            )

        # ----------------------------------------------------
        # SCRIPT
        # ----------------------------------------------------

        speaker_count = len(
            self.speakers
        )

        paragraphs = [
            p.strip()
            for p in re.split(
                r"\n\s*\n",
                script
            )
            if p.strip()
        ]

        for index, paragraph in enumerate(
            paragraphs
        ):

            speaker = self.speakers[
                index % speaker_count
            ]

            sections.append(
                (
                    f"section_{index}",
                    paragraph,
                    speaker
                )
            )

        # ----------------------------------------------------
        # OUTRO
        # ----------------------------------------------------

        if self.outro:

            sections.append(
                (
                    "outro",
                    self.outro,
                    self.speakers[-1]
                )
            )

        combined = AudioSegment.empty()

        total = max(
            len(sections),
            1
        )

        for index, (
            section_name,
            text,
            speaker
        ) in enumerate(sections):

            gender = speaker.get(
                "gender",
                "Male"
            )

            language = speaker.get(
                "language",
                "English"
            )

            voice_name = speaker.get(
                "voice",
                ""
            )

            voice = get_voice(
                language,
                gender,
                voice_name
            )

            raw_file = os.path.join(
                working_dir,
                f"{section_name}.mp3"
            )

            asyncio.run(
                create_tts(
                    text,
                    voice,
                    speech_rate(
                        self.speed
                    ),
                    raw_file
                )
            )

            audio = AudioSegment.from_file(
                raw_file
            )

            if self.ambience_enabled:

                audio = apply_ambience(
                    audio,
                    self.ambience_volume
                )

            combined += audio

            # Tiny natural pause.
            combined += AudioSegment.silent(
                duration=250
            )

            progress = (
                55
                + int(
                    (
                        (index + 1)
                        / total
                    ) * 30
                )
            )

            set_job(
                f"Synthesizing speaker {speaker.get('name', 'Speaker')}...",
                progress / 100
            )

        # ----------------------------------------------------
        # EXPORT MP3
        # ----------------------------------------------------

        set_job(
            "Exporting MP3...",
            0.90
        )

        combined.export(
            final_mp3,
            format="mp3",
            bitrate="128k",
            parameters=[
                "-ac",
                "2",
                "-c:a",
                "libmp3lame"
            ]
        )

        # ----------------------------------------------------
        # EXPORT WAV
        # ----------------------------------------------------

        set_job(
            "Exporting WAV master...",
            0.96
        )

        combined.export(
            final_wav,
            format="wav"
        )

        # ----------------------------------------------------
        # CLEANUP
        # ----------------------------------------------------

        for filename in os.listdir(
            working_dir
        ):

            path = os.path.join(
                working_dir,
                filename
            )

            try:
                os.remove(path)
            except:
                pass

        try:
            os.rmdir(
                working_dir
            )
        except:
            pass

        return final_mp3, final_wav


# ============================================================
# AUTH
# ============================================================

def current_user(
    request: Request
):

    return request.session.get(
        "username"
    )


def auth_required(
    request: Request
):

    if not current_user(request):

        return RedirectResponse(
            "/login",
            status_code=303
        )

    return None


# ============================================================
# HTML / CSS
# ============================================================

CSS = """
<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at top,
            #13213b,
            #070d18 55%
        );
    color: #e5e7eb;
    font-family:
        Inter,
        Arial,
        sans-serif;
    min-height: 100vh;
}

.container {
    width: min(1400px, 96%);
    margin: 25px auto 70px;
}

.card {
    background: rgba(13, 23, 40, .96);
    border: 1px solid #27364d;
    border-radius: 16px;
    padding: 22px;
    margin-bottom: 20px;
    box-shadow:
        0 15px 45px rgba(0,0,0,.28);
}

.brand {
    color: #60a5fa;
}

.muted {
    color: #94a3b8;
}

.success {
    color: #4ade80;
}

.error {
    color: #f87171;
}

h1,
h2,
h3 {
    margin-top: 0;
}

nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 18px;
}

nav a {
    color: #dbeafe;
    background: #172235;
    text-decoration: none;
    padding: 9px 13px;
    border-radius: 8px;
}

nav a:hover {
    background: #243652;
}

label {
    display: block;
    color: #cbd5e1;
    margin-bottom: 7px;
    font-weight: 600;
}

input,
textarea,
select {
    width: 100%;
    background: #07101d;
    color: white;
    border: 1px solid #334155;
    border-radius: 9px;
    padding: 12px;
    margin-bottom: 15px;
    font-size: 15px;
}

textarea {
    min-height: 500px;
    resize: vertical;
    line-height: 1.65;
}

input:focus,
textarea:focus,
select:focus {
    outline: none;
    border-color: #3b82f6;
}

button,
.btn {
    border: 0;
    border-radius: 9px;
    padding: 12px 17px;
    background: #2563eb;
    color: white;
    cursor: pointer;
    font-weight: 700;
    text-decoration: none;
    display: inline-block;
}

button:hover,
.btn:hover {
    background: #1d4ed8;
}

.btn-green {
    background: #15803d;
}

.btn-green:hover {
    background: #166534;
}

.btn-red {
    background: #b91c1c;
}

.btn-gray {
    background: #334155;
}

button:disabled {
    opacity: .5;
    cursor: not-allowed;
}

.toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 15px;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(260px, 1fr)
        );
    gap: 18px;
}

.speaker {
    background: #0a1424;
    border: 1px solid #26364e;
    border-radius: 12px;
    padding: 15px;
}

.speaker h3 {
    color: #60a5fa;
}

.status-box {
    background: #08111f;
    border: 1px solid #26364e;
    border-radius: 12px;
    padding: 17px;
    margin-top: 20px;
}

.progress {
    width: 100%;
    height: 17px;
    border-radius: 999px;
    overflow: hidden;
    background: #1e293b;
}

.progress-bar {
    width: 0%;
    height: 100%;
    background:
        linear-gradient(
            90deg,
            #2563eb,
            #60a5fa
        );
    transition: width .3s;
}

.log {
    background: #091321;
    border: 1px solid #26364e;
    border-radius: 10px;
    padding: 15px;
    margin-bottom: 12px;
}

.range-row {
    display: flex;
    gap: 12px;
    align-items: center;
}

.range-row input {
    margin-bottom: 0;
}

.login {
    max-width: 460px;
    margin: 100px auto;
}

@media(max-width:700px) {

    .container {
        width: 97%;
    }

    .card {
        padding: 16px;
    }

    textarea {
        min-height: 400px;
    }

    nav a {
        flex: 1;
        text-align: center;
    }
}

</style>
"""


# ============================================================
# LOGIN
# ============================================================

@app.get(
    "/login",
    response_class=HTMLResponse
)
def login_page(
    request: Request
):

    if current_user(request):

        return RedirectResponse(
            "/"
        )

    return HTMLResponse(
        f"""
<!DOCTYPE html>

<html>

<head>

<title>
{APP_NAME} Login
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card login">

<h1 class="brand">
🎙 {APP_NAME}
</h1>

<p class="muted">
Secure Podcast Production Gateway
</p>

<form
method="post"
action="/login"
>

<label>
System Operator Username
</label>

<input
name="username"
required
autocomplete="username"
>

<label>
System Password Token Key
</label>

<input
type="password"
name="password"
required
autocomplete="current-password"
>

<button>
Login & Open Workspace
</button>

</form>

</div>

</div>

</body>

</html>
"""
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    username = username.strip()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT password_hash
        FROM users
        WHERE username = ?
        """,
        (username,)
    )

    row = cursor.fetchone()

    conn.close()

    valid = False

    if row:

        try:

            valid = bcrypt.checkpw(
                password.encode("utf-8"),
                row[0].encode("utf-8")
            )

        except:

            valid = False

    if valid:

        request.session["username"] = username

        return RedirectResponse(
            "/",
            status_code=303
        )

    return HTMLResponse(
        f"""
{CSS}

<div class="container">

<div class="card login">

<h2 class="error">
Authentication Failure
</h2>

<p>
Invalid username or password.
</p>

<a
class="btn"
href="/login"
>
Try Again
</a>

</div>

</div>
""",
        status_code=401
    )


@app.get("/logout")
def logout(
    request: Request
):

    request.session.clear()

    return RedirectResponse(
        "/login",
        status_code=303
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def dashboard(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    username = current_user(
        request
    )

    return HTMLResponse(
        f"""
<!DOCTYPE html>

<html>

<head>

<title>
{APP_NAME}
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card">

<div style="
display:flex;
justify-content:space-between;
align-items:center;
flex-wrap:wrap;
gap:15px;
">

<div>

<h1 class="brand">
🎙 {APP_NAME}
</h1>

<p class="muted">
Professional AI Podcast Production Studio
</p>

</div>

<div>
Logged in as:
<strong>
{html.escape(username)}
</strong>
</div>

</div>

<nav>

<a href="/">
Run Workspace
</a>

<a href="/logs">
Server Logs
</a>

<a href="/configuration">
Configurations
</a>

<a href="/users">
User Controls
</a>

<a href="/logout">
Logout
</a>

</nav>

</div>


<!-- ======================================================
     EDITOR
====================================================== -->

<div class="card">

<h2>
Podcast Workspace
</h2>

<div class="toolbar">

<button
type="button"
class="btn-gray"
onclick="clearText()"
>
🗑 Clear Text
</button>

<button
type="button"
class="btn-gray"
onclick="loadSample()"
>
📄 Load Sample
</button>

<button
type="button"
onclick="rewriteText()"
>
✨ AI Rewrite
</button>

</div>

<label>
Topic / Script / Article / Notes
</label>

<textarea
id="article"
placeholder="Paste your article, WhatsApp content, script, topic or notes here..."
></textarea>

<div id="rewriteStatus"
class="muted"
></div>

</div>


<!-- ======================================================
     SPEAKERS
====================================================== -->

<div class="card">

<h2>
🎙 Speakers
</h2>

<label>
Number of Speakers
</label>

<select
id="speakerCount"
onchange="updateSpeakers()"
>

<option value="1">
1 Speaker
</option>

<option value="2">
2 Speakers
</option>

<option value="3">
3 Speakers
</option>

<option value="4">
4 Speakers
</option>

</select>

<div
id="speakers"
class="grid"
></div>

</div>


<!-- ======================================================
     INTRO / OUTRO
====================================================== -->

<div class="card">

<h2>
🎬 Podcast Structure
</h2>

<div class="grid">

<div>

<label>
Intro
</label>

<textarea
id="intro"
style="min-height:130px"
placeholder="Example: Welcome to today's episode of..."
></textarea>

</div>

<div>

<label>
Outro
</label>

<textarea
id="outro"
style="min-height:130px"
placeholder="Example: Thank you for listening..."
></textarea>

</div>

</div>

</div>


<!-- ======================================================
     AUDIO SETTINGS
====================================================== -->

<div class="card">

<h2>
🎚 Audio Settings
</h2>

<div class="grid">

<div>

<label>
Speech Speed
</label>

<select id="speed">

<option value="0.75">
0.75x - Slow
</option>

<option value="0.9">
0.90x
</option>

<option value="1.0" selected>
1.00x - Normal
</option>

<option value="1.1">
1.10x
</option>

<option value="1.25">
1.25x
</option>

<option value="1.5">
1.50x
</option>

</select>

</div>

<div>

<label>
Background Ambience
</label>

<select id="ambience">

<option value="off">
Off
</option>

<option value="on">
Soft Background Ambience
</option>

</select>

</div>

<div>

<label>
Background Volume
</label>

<div class="range-row">

<input
id="ambienceVolume"
type="range"
min="-20"
max="0"
value="-8"
oninput="document.getElementById('volumeValue').innerText=this.value+' dB'"
>

<span id="volumeValue">
-8 dB
</span>

</div>

</div>

</div>

</div>


<!-- ======================================================
     PROCESS
====================================================== -->

<div class="card">

<button
id="processButton"
style="font-size:16px;padding:15px 24px"
onclick="processPodcast()"
>
⚡ Process Podcast
</button>

<div class="status-box">

<strong>
Pipeline Engine Status
</strong>

<p id="pipelineStatus">
Awaiting podcast job...
</p>

<div class="progress">

<div
id="progressBar"
class="progress-bar"
></div>

</div>

<div
id="downloadArea"
style="margin-top:18px"
></div>

</div>

</div>


<script>

const VOICES = {{

    "English": {{

        "Male": [
            ["Andrew", "en-US-AndrewNeural"],
            ["Brian", "en-US-BrianNeural"],
            ["Guy", "en-US-GuyNeural"],
            ["Ryan", "en-GB-RyanNeural"]
        ],

        "Female": [
            ["Jenny", "en-US-JennyNeural"],
            ["Aria", "en-US-AriaNeural"],
            ["Sonia", "en-GB-SoniaNeural"]
        ]

    }},

    "Swahili": {{

        "Male": [
            ["Rafiki", "sw-KE-RafikiNeural"]
        ],

        "Female": [
            ["Zuri", "sw-KE-ZuriNeural"]
        ]

    }},

    "French": {{

        "Male": [
            ["Henri", "fr-FR-HenriNeural"]
        ],

        "Female": [
            ["Denise", "fr-FR-DeniseNeural"]
        ]

    }},

    "Spanish": {{

        "Male": [
            ["Alvaro", "es-ES-AlvaroNeural"]
        ],

        "Female": [
            ["Elvira", "es-ES-ElviraNeural"]
        ]

    }}

}};


function updateSpeakers() {{

    const count =
        parseInt(
            document.getElementById(
                "speakerCount"
            ).value
        );

    const container =
        document.getElementById(
            "speakers"
        );

    container.innerHTML = "";

    for (
        let i = 1;
        i <= count;
        i++
    ) {{

        const card =
            document.createElement(
                "div"
            );

        card.className =
            "speaker";

        card.innerHTML = `
<h3>
Speaker ${{i}}
</h3>

<label>
Speaker Name
</label>

<input
class="speaker-name"
value="Speaker ${{i}}"
>

<label>
Gender
</label>

<select
class="speaker-gender"
onchange="refreshVoice(${{i}})"
id="gender-${{i}}"
>

<option>
Male
</option>

<option>
Female
</option>

</select>

<label>
Language
</label>

<select
class="speaker-language"
onchange="refreshVoice(${{i}})"
id="language-${{i}}"
>

<option>
English
</option>

<option>
Swahili
</option>

<option>
French
</option>

<option>
Spanish
</option>

</select>

<label>
Voice
</label>

<select
class="speaker-voice"
id="voice-${{i}}"
>
</select>
`;

        container.appendChild(
            card
        );

        refreshVoice(i);
    }}

}}


function refreshVoice(i) {{

    const gender =
        document.getElementById(
            "gender-" + i
        ).value;

    const language =
        document.getElementById(
            "language-" + i
        ).value;

    const select =
        document.getElementById(
            "voice-" + i
        );

    select.innerHTML = "";

    const voices =
        VOICES[language][gender];

    voices.forEach(
        function(item) {{

            const option =
                document.createElement(
                    "option"
                );

            option.value =
                item[0];

            option.textContent =
                item[0];

            select.appendChild(
                option
            );

        }}
    );

}}


function clearText() {{

    document.getElementById(
        "article"
    ).value = "";

    document.getElementById(
        "rewriteStatus"
    ).innerText =
        "Editor cleared.";

}}


function loadSample() {{

    document.getElementById(
        "article"
    ).value =
`Welcome to gs_podcast_automation.

This is a sample podcast script.

Today we explore how artificial intelligence can help transform articles, notes and ideas into professional spoken content.

Our production system can rewrite the source material, assign different speakers, synthesize natural voices and export the final production as MP3 or WAV.

Thank you for listening.`;

    document.getElementById(
        "rewriteStatus"
    ).innerText =
        "Sample loaded.";

}}


async function rewriteText() {{

    const text =
        document.getElementById(
            "article"
        ).value.trim();

    const status =
        document.getElementById(
            "rewriteStatus"
        );

    if (!text) {{

        status.innerText =
            "Enter some source text first.";

        return;

    }}

    status.innerText =
        "Gemini is rewriting your script...";

    try {{

        const response =
            await fetch(
                "/rewrite",
                {{

                    method: "POST",

                    headers: {{
                        "Content-Type":
                            "application/json"
                    }},

                    body: JSON.stringify({{
                        text: text
                    }})

                }}
            );

        const data =
            await response.json();

        if (!response.ok) {{

            status.innerText =
                data.detail ||
                "AI rewrite failed.";

            return;

        }}

        document.getElementById(
            "article"
        ).value =
            data.text;

        status.innerText =
            "AI rewrite completed using " +
            data.model;

    }} catch(error) {{

        status.innerText =
            "Rewrite connection error.";

    }}

}}


function collectSpeakers() {{

    const names =
        document.querySelectorAll(
            ".speaker-name"
        );

    const genders =
        document.querySelectorAll(
            ".speaker-gender"
        );

    const languages =
        document.querySelectorAll(
            ".speaker-language"
        );

    const voices =
        document.querySelectorAll(
            ".speaker-voice"
        );

    const speakers = [];

    for (
        let i = 0;
        i < names.length;
        i++
    ) {{

        speakers.push({{

            name:
                names[i].value,

            gender:
                genders[i].value,

            language:
                languages[i].value,

            voice:
                voices[i].value

        }});

    }}

    return speakers;

}}


async function processPodcast() {{

    const text =
        document.getElementById(
            "article"
        ).value.trim();

    if (!text) {{

        alert(
            "Please enter article/script text."
        );

        return;

    }}

    const button =
        document.getElementById(
            "processButton"
        );

    const status =
        document.getElementById(
            "pipelineStatus"
        );

    const progress =
        document.getElementById(
            "progressBar"
        );

    const download =
        document.getElementById(
            "downloadArea"
        );

    button.disabled = true;

    download.innerHTML = "";

    status.innerText =
        "Submitting podcast job...";

    progress.style.width =
        "3%";

    const payload = {{

        text: text,

        intro:
            document.getElementById(
                "intro"
            ).value,

        outro:
            document.getElementById(
                "outro"
            ).value,

        speed:
            document.getElementById(
                "speed"
            ).value,

        ambience:
            document.getElementById(
                "ambience"
            ).value,

        ambience_volume:
            document.getElementById(
                "ambienceVolume"
            ).value,

        speakers:
            collectSpeakers()

    }};

    try {{

        const response =
            await fetch(
                "/process",
                {{

                    method: "POST",

                    headers: {{
                        "Content-Type":
                            "application/json"
                    }},

                    body: JSON.stringify(
                        payload
                    )

                }}
            );

        const data =
            await response.json();

        if (!response.ok) {{

            status.innerText =
                data.detail ||
                "Could not start job.";

            button.disabled = false;

            return;

        }}

        pollJob();

    }} catch(error) {{

        status.innerText =
            "Connection error.";

        button.disabled = false;

    }}

}}


async function pollJob() {{

    const status =
        document.getElementById(
            "pipelineStatus"
        );

    const progress =
        document.getElementById(
            "progressBar"
        );

    const download =
        document.getElementById(
            "downloadArea"
        );

    const button =
        document.getElementById(
            "processButton"
        );

    try {{

        const response =
            await fetch(
                "/job-status"
            );

        const data =
            await response.json();

        status.innerText =
            data.status;

        progress.style.width =
            Math.round(
                data.progress * 100
            ) + "%";

        if (data.running) {{

            setTimeout(
                pollJob,
                1500
            );

            return;

        }}

        button.disabled = false;

        if (data.error) {{

            status.innerHTML =
                '<span class="error">' +
                escapeHtml(
                    data.error
                ) +
                '</span>';

            return;

        }}

        if (data.mp3 || data.wav) {{

            download.innerHTML = `

<h3>
Podcast Ready
</h3>

<div class="toolbar">

${{
    data.mp3
    ?
    '<a class="btn btn-green" href="' +
    data.mp3 +
    '">⬇ Download MP3</a>'
    :
    ''
}}

${{
    data.wav
    ?
    '<a class="btn btn-green" href="' +
    data.wav +
    '">⬇ Download WAV</a>'
    :
    ''
}}

</div>

`;

        }}

    }} catch(error) {{

        status.innerText =
            "Unable to read job status.";

        button.disabled = false;

    }}

}}


function escapeHtml(text) {{

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        text;

    return div.innerHTML;

}}


updateSpeakers();

</script>

</body>

</html>
"""
    )


# ============================================================
# AI REWRITE
# ============================================================

@app.post("/rewrite")
async def rewrite(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    payload = await request.json()

    text = str(
        payload.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return JSONResponse(
            {
                "detail":
                    "Text cannot be empty."
            },
            status_code=400
        )

    try:

        engine = GeminiEngine()

        rewritten, model = (
            engine.rewrite(text)
        )

        return {
            "text": rewritten,
            "model": model
        }

    except Exception as error:

        return JSONResponse(
            {
                "detail":
                    str(error)
            },
            status_code=503
        )


# ============================================================
# PROCESS PODCAST
# ============================================================

@app.post("/process")
async def process(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    username = current_user(
        request
    )

    payload = await request.json()

    text = str(
        payload.get(
            "text",
            ""
        )
    ).strip()

    if not text:

        return JSONResponse(
            {
                "detail":
                    "Podcast text cannot be empty."
            },
            status_code=400
        )

    with job_lock:

        if job_state["running"]:

            return JSONResponse(
                {
                    "detail":
                        "Another podcast job is already running."
                },
                status_code=409
            )

        job_state.update(
            {
                "running": True,
                "progress": 0,
                "status":
                    "Starting podcast pipeline...",
                "file": "",
                "mp3": "",
                "wav": "",
                "error": "",
            }
        )

    def worker():

        try:

            # =================================================
            # AI REWRITE
            # =================================================

            set_job(
                "Gemini AI is preparing the podcast script...",
                0.08
            )

            gemini = GeminiEngine()

            rewritten, model = (
                gemini.rewrite(text)
            )

            set_job(
                f"Script prepared using {model}.",
                0.22
            )

            # =================================================
            # SPEAKERS
            # =================================================

            speakers = payload.get(
                "speakers",
                []
            )

            if not speakers:

                speakers = [
                    {
                        "name": "Speaker 1",
                        "gender": "Male",
                        "language": "English",
                        "voice": "Andrew",
                    }
                ]

            # Limit to 4.
            speakers = speakers[:4]

            # =================================================
            # AUDIO
            # =================================================

            speed = float(
                payload.get(
                    "speed",
                    1.0
                )
            )

            ambience_enabled = (
                payload.get(
                    "ambience",
                    "off"
                ) == "on"
            )

            ambience_volume = float(
                payload.get(
                    "ambience_volume",
                    -8
                )
            )

            audio_engine = (
                PodcastAudioEngine(
                    speakers=speakers,
                    intro=str(
                        payload.get(
                            "intro",
                            ""
                        )
                    ),
                    outro=str(
                        payload.get(
                            "outro",
                            ""
                        )
                    ),
                    ambience_enabled=
                        ambience_enabled,
                    ambience_volume=
                        ambience_volume,
                    speed=speed
                )
            )

            set_job(
                "Synthesizing podcast voices...",
                0.30
            )

            mp3, wav = (
                audio_engine.build(
                    rewritten
                )
            )

            # =================================================
            # COMPLETE
            # =================================================

            set_job(
                "Podcast production completed successfully.",
                1.0
            )

            with job_lock:

                job_state["mp3"] = (
                    "/download/"
                    + os.path.basename(mp3)
                )

                job_state["wav"] = (
                    "/download/"
                    + os.path.basename(wav)
                )

                job_state["file"] = (
                    job_state["mp3"]
                )

                job_state["running"] = False

            # =================================================
            # LOG
            # =================================================

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO logs
                (
                    timestamp,
                    username,
                    status,
                    log_text,
                    saved_file_path
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    username,
                    "COMPLETED",
                    f"Podcast generated with {model}.",
                    mp3
                )
            )

            conn.commit()
            conn.close()

        except Exception as error:

            error_text = str(
                error
            )

            with job_lock:

                job_state["running"] = False
                job_state["progress"] = 0
                job_state["error"] = error_text
                job_state["status"] = (
                    "Pipeline Halt Error: "
                    + error_text
                )

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO logs
                (
                    timestamp,
                    username,
                    status,
                    log_text,
                    saved_file_path
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    username,
                    "FAILED",
                    error_text,
                    ""
                )
            )

            conn.commit()
            conn.close()

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    return {
        "started": True
    }


# ============================================================
# JOB STATUS
# ============================================================

@app.get("/job-status")
def job_status(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    with job_lock:

        return dict(
            job_state
        )


# ============================================================
# DOWNLOAD
# ============================================================

@app.get(
    "/download/{filename}"
)
def download(
    request: Request,
    filename: str
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    filename = os.path.basename(
        filename
    )

    path = os.path.join(
        STORAGE_DIR,
        filename
    )

    if not os.path.isfile(path):

        return HTMLResponse(
            "Podcast file not found.",
            status_code=404
        )

    media_type = (
        "audio/wav"
        if filename.lower().endswith(".wav")
        else "audio/mpeg"
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=filename
    )


# ============================================================
# SERVER LOGS
# ============================================================

@app.get(
    "/logs",
    response_class=HTMLResponse
)
def logs(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            timestamp,
            username,
            status,
            log_text,
            saved_file_path
        FROM logs
        ORDER BY id DESC
        LIMIT 200
        """
    )

    entries = cursor.fetchall()

    conn.close()

    content = ""

    for entry in entries:

        timestamp, username, status, log_text, file_path = entry

        css_class = (
            "success"
            if status == "COMPLETED"
            else "error"
        )

        download_link = ""

        if file_path:

            filename = os.path.basename(
                file_path
            )

            if os.path.exists(
                file_path
            ):

                download_link = f"""
<a
class="btn btn-green"
href="/download/{html.escape(filename)}"
>
⬇ Download
</a>
"""

        content += f"""
<div class="log">

<strong>
{html.escape(str(timestamp))}
</strong>

<p class="muted">
Operator:
{html.escape(str(username))}
</p>

<p class="{css_class}">
Status:
<strong>
{html.escape(str(status))}
</strong>
</p>

<p>
{html.escape(str(log_text))}
</p>

{download_link}

</div>
"""

    if not content:

        content = """
<p class="muted">
No execution logs yet.
</p>
"""

    return HTMLResponse(
        f"""
<!DOCTYPE html>

<html>

<head>

<title>
Server Logs
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card">

<h1 class="brand">
Server Logs
</h1>

<nav>

<a href="/">
Run Workspace
</a>

<a href="/configuration">
Configurations
</a>

<a href="/users">
User Controls
</a>

<a href="/logout">
Logout
</a>

</nav>

</div>

<div class="card">

<h2>
Execution Logs
</h2>

{content}

</div>

</div>

</body>

</html>
"""
    )


# ============================================================
# CONFIGURATION
# ============================================================

@app.get(
    "/configuration",
    response_class=HTMLResponse
)
def configuration(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    gemini_key = pull_config(
        "gemini_api_key",
        ""
    )

    cc_key = pull_config(
        "cloudconvert_api_key",
        ""
    )

    use_cc = (
        pull_config(
            "use_cloudconvert",
            "False"
        ) == "True"
    )

    checked = (
        "checked"
        if use_cc
        else ""
    )

    return HTMLResponse(
        f"""
<!DOCTYPE html>

<html>

<head>

<title>
Configurations
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card">

<h1 class="brand">
Configurations
</h1>

<nav>

<a href="/">
Run Workspace
</a>

<a href="/logs">
Server Logs
</a>

<a href="/users">
User Controls
</a>

<a href="/logout">
Logout
</a>

</nav>

</div>

<div class="card">

<h2>
AI Configuration
</h2>

<form
method="post"
action="/configuration"
>

<label>
Google Gemini API Key
</label>

<input
type="password"
name="gemini_api_key"
value="{html.escape(gemini_key)}"
autocomplete="off"
>

<p class="muted">

Primary:
<strong>
Gemini 3.7 Flash
</strong>

<br>

Automatic fallback:
Gemini 3.6 Flash →
Gemini 3.5 Flash

</p>

<h2>
CloudConvert
</h2>

<label>

<input
type="checkbox"
name="use_cloudconvert"
{checked}
style="width:auto"
>

 Route via CloudConvert APIs

</label>

<label>
CloudConvert API Key
</label>

<input
type="password"
name="cloudconvert_api_key"
value="{html.escape(cc_key)}"
autocomplete="off"
>

<button
class="btn-green"
>
💾 Save Parameters
</button>

</form>

</div>

</div>

</body>

</html>
"""
    )


@app.post("/configuration")
def save_configuration(
    request: Request,
    gemini_api_key: str = Form(""),
    cloudconvert_api_key: str = Form(""),
    use_cloudconvert: Optional[str] = Form(None)
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    push_config(
        "gemini_api_key",
        gemini_api_key.strip()
    )

    push_config(
        "cloudconvert_api_key",
        cloudconvert_api_key.strip()
    )

    push_config(
        "use_cloudconvert",
        "True"
        if use_cloudconvert
        else "False"
    )

    return RedirectResponse(
        "/configuration",
        status_code=303
    )


# ============================================================
# USERS
# ============================================================

@app.get(
    "/users",
    response_class=HTMLResponse
)
def users(
    request: Request
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, username
        FROM users
        ORDER BY username
        """
    )

    entries = cursor.fetchall()

    conn.close()

    users_html = ""

    for user_id, username in entries:

        users_html += f"""
<div class="log">

<strong>
{html.escape(username)}
</strong>

<span class="muted">
ID: {user_id}
</span>

</div>
"""

    return HTMLResponse(
        f"""
<!DOCTYPE html>

<html>

<head>

<title>
User Controls
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card">

<h1 class="brand">
User Controls
</h1>

<nav>

<a href="/">
Run Workspace
</a>

<a href="/logs">
Server Logs
</a>

<a href="/configuration">
Configurations
</a>

<a href="/logout">
Logout
</a>

</nav>

</div>

<div class="grid">

<div class="card">

<h2>
Register New User
</h2>

<form
method="post"
action="/users"
>

<label>
New Username ID
</label>

<input
name="username"
required
>

<label>
New Password
</label>

<input
name="password"
type="password"
required
>

<button
class="btn-green"
>
Create User
</button>

</form>

</div>

<div class="card">

<h2>
Registered Users
</h2>

{users_html}

</div>

</div>

</div>

</body>

</html>
"""
    )


@app.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):

    redirect = auth_required(
        request
    )

    if redirect:
        return redirect

    username = username.strip()

    hashed = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    conn = get_db()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO users
            (username, password_hash)
            VALUES (?, ?)
            """,
            (
                username,
                hashed
            )
        )

        conn.commit()

    except sqlite3.IntegrityError:

        conn.close()

        return HTMLResponse(
            f"""
{CSS}

<div class="container">

<div class="card">

<h2 class="error">
Username already exists.
</h2>

<a
class="btn"
href="/users"
>
Back
</a>

</div>

</div>
""",
            status_code=409
        )

    conn.close()

    return RedirectResponse(
        "/users",
        status_code=303
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    database_provisioner()


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "8080"
        )
    )

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port
    )
