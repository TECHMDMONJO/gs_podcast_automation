import os
import time
import sqlite3
import threading
import html
import secrets
from typing import Optional

import bcrypt
import uvicorn

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
    JSONResponse,
)
from starlette.middleware.sessions import SessionMiddleware

from gtts import gTTS
from pydub import AudioSegment

# ============================================================
# GEMINI - CURRENT GOOGLE GENAI SDK
# ============================================================

from google import genai


# ============================================================
# APPLICATION CONFIG
# ============================================================

APP_NAME = "gs_podcast_automation"

DB_DIR = "instance"
DB_PATH = os.path.join(DB_DIR, "data.db")

STORAGE_DIR = os.path.join("storage", "outputs")

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version="2.0.0",
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
# DATABASE
# ============================================================

def get_db():
    return sqlite3.connect(
        DB_PATH,
        timeout=30,
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

    # --------------------------------------------------------
    # Initial admin
    # --------------------------------------------------------

    cursor.execute(
        "SELECT id FROM users WHERE username = ?",
        ("admin",)
    )

    if not cursor.fetchone():

        initial_password = os.getenv(
            "ADMIN_PASSWORD",
            "smbagathi"
        )

        hashed = bcrypt.hashpw(
            initial_password.encode("utf-8"),
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
                hashed.decode("utf-8"),
            )
        )

    conn.commit()
    conn.close()


def pull_config(
    key: str,
    default_val: str = ""
) -> str:

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
    value_str: str
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
            value_str,
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# JOB STATE
# ============================================================

job_state = {
    "running": False,
    "progress": 0.0,
    "status": "System Pipeline Engine Status: Awaiting Job...",
    "file": "",
    "error": "",
}

job_lock = threading.Lock()


def update_job_state(
    status_message: str,
    progress: float
):

    with job_lock:

        job_state["status"] = status_message
        job_state["progress"] = progress

        if progress >= 1.0:
            job_state["running"] = False


# ============================================================
# PODCAST ENGINE
# ============================================================

class ProductionPodcastEngine:

    MODEL_NAME = "gemini-3.7-flash"

    def __init__(self):

        self.gemini_key = (
            pull_config("gemini_api_key")
            or os.getenv("GEMINI_API_KEY", "")
        )

        self.use_cc = (
            pull_config(
                "use_cloudconvert",
                "False"
            ) == "True"
        )

        self.cc_key = (
            pull_config("cloudconvert_api_key")
            or os.getenv(
                "CLOUDCONVERT_API_KEY",
                ""
            )
        )

        self.client = None

        if self.gemini_key:

            self.client = genai.Client(
                api_key=self.gemini_key
            )

    def execute_workflow_pipeline(
        self,
        raw_text: str,
        operator_name: str,
        status_callback
    ) -> str:

        timestamp = int(time.time())

        temp_raw_audio = os.path.join(
            STORAGE_DIR,
            f"raw_{timestamp}.mp3"
        )

        final_processed_audio = os.path.join(
            STORAGE_DIR,
            f"podcast_{timestamp}.mp3"
        )

        try:

            # =================================================
            # VALIDATE GEMINI
            # =================================================

            if not self.gemini_key:

                raise Exception(
                    "Gemini API key is not configured."
                )

            if self.client is None:

                raise Exception(
                    "Gemini client could not be initialized."
                )

            # =================================================
            # GEMINI SCRIPT GENERATION
            # =================================================

            status_callback(
                "Gemini 3.7 Flash is rewriting the article...",
                0.15
            )

            prompt = """
You are the editorial engine for gs_podcast_automation.

Transform the supplied article into a polished, natural,
broadcast-quality podcast narration script.

Requirements:

- Preserve the author's core argument and meaning.
- Do not invent facts.
- Do not introduce unsupported claims.
- Remove awkward newspaper formatting.
- Remove excessive paragraph breaks.
- Make the language natural when spoken aloud.
- Keep important names, figures and arguments accurate.
- Use smooth transitions.
- Do not add an introduction that changes the article's meaning.
- Do not add a conclusion that is not supported by the source.
- Produce ONLY the final narration script.

SOURCE ARTICLE:

""" + raw_text

            response = self.client.models.generate_content(
                model=self.MODEL_NAME,
                contents=prompt,
            )

            podcast_script = response.text

            if not podcast_script:

                raise Exception(
                    "Gemini returned an empty response."
                )

            # =================================================
            # TEXT TO SPEECH
            # =================================================

            status_callback(
                "Generating podcast narration audio...",
                0.40
            )

            tts_client = gTTS(
                text=podcast_script,
                lang="en",
                slow=False
            )

            tts_client.save(
                temp_raw_audio
            )

            # =================================================
            # AUDIO PROCESSING
            # =================================================

            status_callback(
                "Processing and mastering MP3 audio...",
                0.65
            )

            native_audio = AudioSegment.from_mp3(
                temp_raw_audio
            )

            # Mono output for efficient podcast delivery.
            native_audio = native_audio.set_channels(1)

            native_audio.export(
                final_processed_audio,
                format="mp3",
                bitrate="64k",
                parameters=[
                    "-ac",
                    "1",
                    "-c:a",
                    "libmp3lame",
                ],
            )

            # =================================================
            # CLEANUP
            # =================================================

            if os.path.exists(temp_raw_audio):

                os.remove(
                    temp_raw_audio
                )

            status_callback(
                "Automation sequence successful.",
                1.0
            )

            return final_processed_audio

        except Exception as error_fault:

            if os.path.exists(temp_raw_audio):

                os.remove(
                    temp_raw_audio
                )

            status_callback(
                f"Pipeline Halt Error: {str(error_fault)}",
                0.0
            )

            return ""


# ============================================================
# AUTHENTICATION
# ============================================================

def current_user(
    request: Request
) -> Optional[str]:

    return request.session.get(
        "username"
    )


def require_login(
    request: Request
):

    if not current_user(request):

        return RedirectResponse(
            "/login",
            status_code=303
        )

    return None


# ============================================================
# CSS
# ============================================================

CSS = """
<style>

:root {
    --bg: #070d18;
    --card: #101827;
    --card2: #0c1422;
    --border: #263449;
    --blue: #3b82f6;
    --blue-dark: #1d4ed8;
    --green: #16a34a;
    --red: #dc2626;
    --text: #e5e7eb;
    --muted: #94a3b8;
}

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at top,
            #101c32 0,
            var(--bg) 45%
        );
    color: var(--text);
    font-family:
        Inter,
        Arial,
        Helvetica,
        sans-serif;
    min-height: 100vh;
}

.container {
    width: min(1200px, 94%);
    margin: 30px auto 60px;
}

.card {
    background: rgba(16, 24, 39, .94);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow:
        0 15px 45px rgba(0,0,0,.25);
}

.login-card {
    max-width: 460px;
    margin: 100px auto;
}

.brand {
    color: #60a5fa;
}

h1,
h2,
h3 {
    margin-top: 0;
}

label {
    display: block;
    color: var(--muted);
    margin-bottom: 8px;
}

input,
textarea,
select {
    width: 100%;
    padding: 13px;
    margin-bottom: 16px;
    border-radius: 9px;
    border: 1px solid #344155;
    background: #09111f;
    color: white;
    outline: none;
}

textarea {
    min-height: 240px;
    resize: vertical;
    line-height: 1.55;
}

input:focus,
textarea:focus {
    border-color: var(--blue);
}

button,
.btn {
    display: inline-block;
    border: 0;
    border-radius: 9px;
    padding: 12px 18px;
    background: var(--blue);
    color: white;
    cursor: pointer;
    text-decoration: none;
    font-weight: 600;
}

button:hover,
.btn:hover {
    background: var(--blue-dark);
}

button:disabled {
    opacity: .55;
    cursor: not-allowed;
}

.btn-green {
    background: var(--green);
}

.btn-red {
    background: var(--red);
}

nav {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

nav a {
    padding: 9px 13px;
    background: #172235;
    color: #dbeafe;
    border-radius: 8px;
    text-decoration: none;
}

nav a:hover {
    background: #22324c;
}

.status {
    margin-top: 20px;
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
}

.progress {
    width: 100%;
    height: 16px;
    background: #1f2937;
    border-radius: 999px;
    overflow: hidden;
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
    transition: width .4s ease;
}

.log {
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 12px;
}

.success {
    color: #4ade80;
}

.error {
    color: #f87171;
}

.muted {
    color: var(--muted);
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(300px, 1fr)
        );
    gap: 20px;
}

.stat {
    background: #0b1423;
    border: 1px solid var(--border);
    padding: 16px;
    border-radius: 10px;
}

.stat strong {
    display: block;
    font-size: 24px;
    color: #60a5fa;
}

.checkbox-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 15px;
}

.checkbox-row input {
    width: auto;
    margin: 0;
}

@media(max-width: 650px) {

    .container {
        width: 96%;
        margin-top: 15px;
    }

    .card {
        padding: 17px;
    }

    nav a {
        width: 100%;
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
            "/",
            status_code=303
        )

    return HTMLResponse(
        f"""
<!DOCTYPE html>
<html>

<head>

<title>
{APP_NAME} - Login
</title>

{CSS}

</head>

<body>

<div class="container">

<div class="card login-card">

<h1 class="brand">
🎙 {APP_NAME}
</h1>

<p class="muted">
Secure Gateway
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
name="password"
type="password"
required
autocomplete="current-password"
>

<button type="submit">
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

        except Exception:

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

<div class="card login-card">

<h2 class="error">
Authentication Failure
</h2>

<p class="muted">
Invalid username or password.
</p>

<a class="btn" href="/login">
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
# MAIN DASHBOARD
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def dashboard(
    request: Request
):

    redirect = require_login(request)

    if redirect:
        return redirect

    username = current_user(request)

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
gap:20px;
align-items:center;
flex-wrap:wrap;
">

<div>

<h1 class="brand">
🎙 {APP_NAME}
</h1>

<p class="muted">
AI-powered podcast production workspace
</p>

</div>

<div>

<span class="muted">
Logged in as:
</span>

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


<div class="card">

<h2>
Run Workspace
</h2>

<label>
WhatsApp Content / Article Input Buffer
</label>

<textarea
id="article"
placeholder="Paste your article, WhatsApp content or source material here..."
></textarea>

<button
id="runButton"
onclick="startProcessing()"
>
⚡ Process Input Stream
</button>

<div class="status">

<strong>
Pipeline Engine Status
</strong>

<p id="status">
Awaiting Job...
</p>

<div class="progress">

<div
id="progressBar"
class="progress-bar"
></div>

</div>

<p id="result"></p>

</div>

</div>


<div class="grid">

<div class="stat">

<strong>
Gemini 3.7
</strong>

<span class="muted">
Latest Flash production model
</span>

</div>

<div class="stat">

<strong>
gTTS
</strong>

<span class="muted">
Podcast narration synthesis
</span>

</div>

<div class="stat">

<strong>
MP3
</strong>

<span class="muted">
64 kbps mono output
</span>

</div>

</div>

</div>


<script>

async function startProcessing() {{

    const article =
        document.getElementById(
            "article"
        ).value.trim();

    const button =
        document.getElementById(
            "runButton"
        );

    const status =
        document.getElementById(
            "status"
        );

    const progress =
        document.getElementById(
            "progressBar"
        );

    const result =
        document.getElementById(
            "result"
        );

    if (!article) {{

        status.innerText =
            "Please enter article content first.";

        return;
    }}

    button.disabled = true;

    result.innerHTML = "";

    status.innerText =
        "Submitting podcast job...";

    progress.style.width = "5%";

    const formData =
        new FormData();

    formData.append(
        "article",
        article
    );

    try {{

        const response =
            await fetch(
                "/process",
                {{
                    method: "POST",
                    body: formData
                }}
            );

        const data =
            await response.json();

        if (!response.ok) {{

            status.innerText =
                data.detail ||
                "Unable to start job.";

            button.disabled = false;

            return;
        }}

        pollJob();

    }} catch(error) {{

        status.innerText =
            "Connection error: " +
            error;

        button.disabled = false;

    }}

}}


async function pollJob() {{

    const status =
        document.getElementById(
            "status"
        );

    const progress =
        document.getElementById(
            "progressBar"
        );

    const result =
        document.getElementById(
            "result"
        );

    const button =
        document.getElementById(
            "runButton"
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

        if (data.file) {{

            result.innerHTML =
                '<br><a class="btn btn-green" href="' +
                data.file +
                '">⬇ Download Podcast MP3</a>';

        }}

        if (data.error) {{

            result.innerHTML =
                '<br><span class="error">' +
                escapeHtml(data.error) +
                '</span>';

        }}

    }} catch(error) {{

        status.innerText =
            "Status connection failed.";

        button.disabled = false;

    }}

}}


function escapeHtml(text) {{

    const div =
        document.createElement(
            "div"
        );

    div.textContent = text;

    return div.innerHTML;

}}

</script>

</body>
</html>
"""
    )


# ============================================================
# PROCESS JOB
# ============================================================

@app.post("/process")
async def process_article(
    request: Request,
    article: str = Form(...)
):

    redirect = require_login(request)

    if redirect:
        return redirect

    username = current_user(request)

    article = article.strip()

    if not article:

        return JSONResponse(
            {
                "detail":
                    "Article content cannot be empty."
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

        job_state["running"] = True
        job_state["progress"] = 0.0
        job_state["status"] = "Job queued..."
        job_state["file"] = ""
        job_state["error"] = ""

    def worker():

        engine = ProductionPodcastEngine()

        saved_path = (
            engine.execute_workflow_pipeline(
                article,
                username,
                update_job_state
            )
        )

        if saved_path:

            status = "COMPLETED"

            log_text = (
                "Podcast processing completed successfully."
            )

        else:

            status = "FAILED"

            log_text = job_state.get(
                "status",
                "Podcast processing failed."
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
                status,
                log_text,
                saved_path,
            )
        )

        conn.commit()
        conn.close()

        with job_lock:

            if saved_path:

                job_state["file"] = (
                    "/download/"
                    + os.path.basename(
                        saved_path
                    )
                )

            job_state["running"] = False

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
def get_job_status(
    request: Request
):

    redirect = require_login(request)

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
def download_file(
    request: Request,
    filename: str
):

    redirect = require_login(request)

    if redirect:
        return redirect

    safe_filename = os.path.basename(
        filename
    )

    path = os.path.join(
        STORAGE_DIR,
        safe_filename
    )

    if not os.path.isfile(path):

        return HTMLResponse(
            "File not found.",
            status_code=404
        )

    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=safe_filename
    )


# ============================================================
# SERVER LOGS
# ============================================================

@app.get(
    "/logs",
    response_class=HTMLResponse
)
def logs_page(
    request: Request
):

    redirect = require_login(request)

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

    logs = cursor.fetchall()

    conn.close()

    log_html = ""

    for entry in logs:

        timestamp, username, status, log_text, saved_file = entry

        status_class = (
            "success"
            if status == "COMPLETED"
            else "error"
        )

        download = ""

        if (
            saved_file
            and os.path.exists(saved_file)
        ):

            filename = os.path.basename(
                saved_file
            )

            download = f"""
<a
class="btn btn-green"
href="/download/{html.escape(filename)}"
>
⬇ Download MP3
</a>
"""

        log_html += f"""
<div class="log">

<strong>
{html.escape(str(timestamp))}
</strong>

<p class="muted">
Operator:
{html.escape(str(username))}
</p>

<p class="{status_class}">
Outcome:
<strong>
{html.escape(str(status))}
</strong>
</p>

<p>
{html.escape(str(log_text))}
</p>

{download}

</div>
"""

    if not log_html:

        log_html = """
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
Dashboard
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

{log_html}

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
def configuration_page(
    request: Request
):

    redirect = require_login(request)

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
Dashboard
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
AI & Audio Configuration
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
Current model:
<strong>
gemini-3.7-flash
</strong>
</p>

<div class="checkbox-row">

<input
type="checkbox"
name="use_cloudconvert"
{checked}
>

<span>
Route audio through CloudConvert
</span>

</div>

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
type="submit"
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
    use_cloudconvert: Optional[str] = Form(None),
):

    redirect = require_login(request)

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
# USER MANAGEMENT
# ============================================================

@app.get(
    "/users",
    response_class=HTMLResponse
)
def users_page(
    request: Request
):

    redirect = require_login(request)

    if redirect:
        return redirect

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            id,
            username
        FROM users
        ORDER BY username
        """
    )

    users = cursor.fetchall()

    conn.close()

    users_html = ""

    for user_id, username in users:

        users_html += f"""
<div class="log">

<strong>
{html.escape(username)}
</strong>

<span class="muted">
User ID: {user_id}
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
Dashboard
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
type="submit"
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

    redirect = require_login(request)

    if redirect:
        return redirect

    username = username.strip()

    if not username or not password:

        return HTMLResponse(
            "Username and password are required.",
            status_code=400
        )

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
Back to User Controls
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
def startup_event():

    database_provisioner()


# ============================================================
# LOCAL DEVELOPMENT
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
