import os
import time
import sqlite3
import threading
import html
from typing import Optional

import bcrypt
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from starlette.middleware.sessions import SessionMiddleware

from gtts import gTTS
from pydub import AudioSegment

# Gemini
try:
    import google.generativeai as genai
except ImportError:
    genai = None


# ============================================================
# CONFIGURATION
# ============================================================

DB_DIR = "instance"
DB_PATH = os.path.join(DB_DIR, "data.db")

STORAGE_DIR = os.path.join("storage", "outputs")

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(title="gs_podcast_automation")

SESSION_SECRET = os.getenv(
    "SESSION_SECRET",
    "change-this-secret-in-render-environment"
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
    return sqlite3.connect(DB_PATH)


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
        "SELECT * FROM users WHERE username = ?",
        ("admin",)
    )

    if not cursor.fetchone():
        hashed_bytes = bcrypt.hashpw(
            "smbagathi".encode("utf-8"),
            bcrypt.gensalt()
        )

        cursor.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (?, ?)
            """,
            ("admin", hashed_bytes.decode("utf-8"))
        )

    conn.commit()
    conn.close()


def pull_config(key: str, default_val: str = "") -> str:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT value FROM configs WHERE key = ?",
        (key,)
    )

    row = cursor.fetchone()
    conn.close()

    return row[0] if row else default_val


def push_config(key: str, value_str: str):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO configs (key, value)
        VALUES (?, ?)
        """,
        (key, value_str)
    )

    conn.commit()
    conn.close()


# ============================================================
# GEMINI / PODCAST ENGINE
# ============================================================

class ProductionPodcastEngine:

    def __init__(self):

        self.gemini_key = pull_config(
            "gemini_api_key",
            ""
        )

        self.use_cc = (
            pull_config(
                "use_cloudconvert",
                "False"
            ) == "True"
        )

        self.cc_key = pull_config(
            "cloudconvert_api_key",
            ""
        )

        if self.gemini_key and genai:
            genai.configure(
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

            # ------------------------------------------------
            # Gemini
            # ------------------------------------------------

            if not self.gemini_key:
                raise Exception(
                    "Google Gemini API key is not configured."
                )

            if genai is None:
                raise Exception(
                    "google-generativeai package is not installed."
                )

            status_callback(
                "Gemini AI is parsing and rewriting text...",
                0.20
            )

            model = genai.GenerativeModel(
                "gemini-1.5-flash"
            )

            response = model.generate_content(
                "Rewrite the following content into a natural, "
                "engaging podcast script. Keep the meaning accurate "
                "and make it suitable for spoken narration.\n\n"
                + raw_text
            )

            podcast_script = response.text

            if not podcast_script:
                raise Exception(
                    "Gemini returned an empty response."
                )

            # ------------------------------------------------
            # TTS
            # ------------------------------------------------

            status_callback(
                "Synthesizing podcast audio...",
                0.45
            )

            tts_client = gTTS(
                text=podcast_script,
                lang="en",
                slow=False
            )

            tts_client.save(temp_raw_audio)

            # ------------------------------------------------
            # Audio processing
            # ------------------------------------------------

            status_callback(
                "Executing local FFmpeg audio pipeline...",
                0.70
            )

            native_audio_segment = (
                AudioSegment.from_mp3(temp_raw_audio)
            )

            native_audio_segment.export(
                final_processed_audio,
                format="mp3",
                bitrate="64k",
                parameters=[
                    "-ac",
                    "1",
                    "-c:a",
                    "libmp3lame"
                ]
            )

            if os.path.exists(temp_raw_audio):
                os.remove(temp_raw_audio)

            status_callback(
                "Automation sequence successful.",
                1.0
            )

            return final_processed_audio

        except Exception as error_fault:

            if os.path.exists(temp_raw_audio):
                os.remove(temp_raw_audio)

            status_callback(
                f"Pipeline Halt Error: {str(error_fault)}",
                0.0
            )

            return ""


# ============================================================
# JOB STATE
# ============================================================

job_state = {
    "running": False,
    "progress": 0,
    "status": "System Pipeline Engine Status: Awaiting Job...",
    "file": "",
    "error": ""
}

job_lock = threading.Lock()


def update_job_state(message: str, progress: float):

    with job_lock:

        job_state["status"] = message
        job_state["progress"] = progress

        if progress >= 1:
            job_state["running"] = False


# ============================================================
# AUTHENTICATION HELPERS
# ============================================================

def current_user(request: Request) -> Optional[str]:
    return request.session.get("username")


def require_login(request: Request):

    if not current_user(request):
        return RedirectResponse(
            "/login",
            status_code=303
        )

    return None


# ============================================================
# HTML STYLING
# ============================================================

CSS = """
<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #0b1220;
    color: #e5e7eb;
    font-family: Arial, Helvetica, sans-serif;
}

.container {
    width: min(1100px, 94%);
    margin: 35px auto;
}

.card {
    background: #111827;
    border: 1px solid #263244;
    border-radius: 14px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: 0 10px 30px rgba(0,0,0,.25);
}

.login-card {
    max-width: 450px;
    margin: 100px auto;
}

h1, h2, h3 {
    margin-top: 0;
}

.brand {
    color: #60a5fa;
}

label {
    display: block;
    margin-bottom: 7px;
    color: #9ca3af;
}

input,
textarea {
    width: 100%;
    padding: 12px;
    border-radius: 8px;
    border: 1px solid #374151;
    background: #0f172a;
    color: white;
    margin-bottom: 15px;
}

textarea {
    min-height: 180px;
    resize: vertical;
}

button,
.btn {
    border: none;
    border-radius: 8px;
    padding: 12px 18px;
    background: #2563eb;
    color: white;
    cursor: pointer;
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

.btn-red {
    background: #b91c1c;
}

nav {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 20px;
}

nav a {
    color: #dbeafe;
    text-decoration: none;
    padding: 9px 13px;
    border-radius: 7px;
    background: #1e293b;
}

.status {
    margin-top: 15px;
    padding: 15px;
    border-radius: 8px;
    background: #0f172a;
}

.progress {
    width: 100%;
    height: 15px;
    background: #1f2937;
    border-radius: 10px;
    overflow: hidden;
    margin-top: 12px;
}

.progress-bar {
    height: 100%;
    width: 0%;
    background: #3b82f6;
    transition: width .3s;
}

.log {
    border: 1px solid #263244;
    padding: 14px;
    border-radius: 8px;
    margin-bottom: 10px;
    background: #0f172a;
}

.success {
    color: #4ade80;
}

.error {
    color: #f87171;
}

.muted {
    color: #9ca3af;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 20px;
}

</style>
"""


# ============================================================
# LOGIN PAGE
# ============================================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):

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
            <title>gs_podcast_automation</title>
            {CSS}
        </head>

        <body>

        <div class="container">

            <div class="card login-card">

                <h1 class="brand">
                    🎙 gs_podcast_automation
                </h1>

                <p class="muted">
                    Secure Gateway
                </p>

                <form method="post" action="/login">

                    <label>System Operator Username</label>

                    <input
                        name="username"
                        required
                        autocomplete="username"
                    >

                    <label>System Password Token Key</label>

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

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT password_hash
        FROM users
        WHERE username = ?
        """,
        (username.strip(),)
    )

    row = cursor.fetchone()
    conn.close()

    if row:

        try:
            valid = bcrypt.checkpw(
                password.encode("utf-8"),
                row[0].encode("utf-8")
            )
        except Exception:
            valid = False

        if valid:

            request.session["username"] = username.strip()

            return RedirectResponse(
                "/",
                status_code=303
            )

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html>
        <head>{CSS}</head>
        <body>

        <div class="container">

        <div class="card login-card">

            <h1 class="brand">
                🎙 gs_podcast_automation
            </h1>

            <p class="error">
                Authentication Failure.
            </p>

            <form method="post" action="/login">

                <label>Username</label>
                <input name="username" required>

                <label>Password</label>
                <input
                    name="password"
                    type="password"
                    required
                >

                <button type="submit">
                    Login
                </button>

            </form>

        </div>

        </div>

        </body>
        </html>
        """
    )


@app.get("/logout")
def logout(request: Request):

    request.session.clear()

    return RedirectResponse(
        "/login",
        status_code=303
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):

    redirect = require_login(request)

    if redirect:
        return redirect

    username = current_user(request)

    return HTMLResponse(
        f"""
        <!DOCTYPE html>

        <html>

        <head>

            <title>gs_podcast_automation Dashboard</title>

            {CSS}

        </head>

        <body>

        <div class="container">

            <div class="card">

                <h1 class="brand">
                    🎙 gs_podcast_automation
                </h1>

                <p class="muted">
                    Logged in as:
                    <strong>{html.escape(username)}</strong>
                </p>

                <nav>
                    <a href="/">Run Workspace</a>
                    <a href="/logs">Server Logs</a>
                    <a href="/configuration">Configurations</a>
                    <a href="/users">User Controls</a>
                    <a href="/logout">Logout</a>
                </nav>

            </div>

            <div class="card">

                <h2>Run Workspace</h2>

                <form
                    method="post"
                    action="/process"
                    onsubmit="startProcessing(event)"
                >

                    <label>
                        WhatsApp Content / Article Input Buffer
                    </label>

                    <textarea
                        id="article"
                        name="article"
                        placeholder="Paste your article or WhatsApp content here..."
                        required
                    ></textarea>

                    <button
                        id="runButton"
                        type="submit"
                    >
                        ⚡ Process Input Stream
                    </button>

                </form>

                <div class="status">

                    <strong>Pipeline Status</strong>

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

        </div>

        <script>

        async function startProcessing(event) {{

            event.preventDefault();

            const article =
                document.getElementById("article").value;

            const button =
                document.getElementById("runButton");

            const status =
                document.getElementById("status");

            const progress =
                document.getElementById("progressBar");

            const result =
                document.getElementById("result");

            button.disabled = true;

            result.innerHTML = "";

            status.innerText =
                "Starting podcast processing...";

            progress.style.width = "5%";

            const formData = new FormData();

            formData.append("article", article);

            const response = await fetch(
                "/process",
                {{
                    method: "POST",
                    body: formData
                }}
            );

            const data = await response.json();

            if (!response.ok) {{

                status.innerText =
                    data.detail || "Unable to start job.";

                button.disabled = false;

                return;
            }}

            pollJob();

        }}

        async function pollJob() {{

            const status =
                document.getElementById("status");

            const progress =
                document.getElementById("progressBar");

            const result =
                document.getElementById("result");

            const button =
                document.getElementById("runButton");

            const response =
                await fetch("/job-status");

            const data =
                await response.json();

            status.innerText = data.status;

            progress.style.width =
                Math.round(data.progress * 100) + "%";

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
                    '<a class="btn btn-green" href="' +
                    data.file +
                    '">⬇ Download Podcast</a>';

            }}

            if (data.error) {{

                result.innerHTML =
                    '<span class="error">' +
                    data.error +
                    '</span>';

            }}

        }}

        </script>

        </body>

        </html>
        """
    )


# ============================================================
# START PROCESSING
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
        return {
            "detail": "Article content cannot be empty."
        }

    with job_lock:

        if job_state["running"]:

            return {
                "detail": "Another podcast job is already running."
            }

        job_state["running"] = True
        job_state["progress"] = 0
        job_state["status"] = "Job queued..."
        job_state["file"] = ""
        job_state["error"] = ""

    def worker():

        engine = ProductionPodcastEngine()

        saved_path = engine.execute_workflow_pipeline(
            article,
            username,
            update_job_state
        )

        conn = get_db()
        cursor = conn.cursor()

        if saved_path:

            status = "COMPLETED"
            log_text = "Podcast processing completed."

        else:

            status = "FAILED"
            log_text = job_state.get(
                "status",
                "Podcast processing failed."
            )

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
                saved_path
            )
        )

        conn.commit()
        conn.close()

        with job_lock:

            job_state["file"] = (
                f"/download/{os.path.basename(saved_path)}"
                if saved_path
                else ""
            )

            if not saved_path:

                job_state["error"] = (
                    job_state.get(
                        "status",
                        "Pipeline failed."
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
def get_job_status(request: Request):

    redirect = require_login(request)

    if redirect:
        return redirect

    with job_lock:

        return {
            "running": job_state["running"],
            "progress": job_state["progress"],
            "status": job_state["status"],
            "file": job_state["file"],
            "error": job_state["error"]
        }


# ============================================================
# DOWNLOAD
# ============================================================

@app.get("/download/{filename}")
def download_file(
    request: Request,
    filename: str
):

    redirect = require_login(request)

    if redirect:
        return redirect

    # Prevent directory traversal.
    safe_filename = os.path.basename(filename)

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
# LOGS
# ============================================================

@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):

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
        LIMIT 100
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

        if saved_file and os.path.exists(saved_file):

            filename = os.path.basename(saved_file)

            download = f"""
                <p>
                    <a
                        class="btn btn-green"
                        href="/download/{html.escape(filename)}"
                    >
                        Download
                    </a>
                </p>
            """

        log_html += f"""
        <div class="log">

            <strong>
                {html.escape(str(timestamp))}
            </strong>

            <p>
                User:
                {html.escape(str(username))}
            </p>

            <p class="{status_class}">
                Outcome:
                {html.escape(str(status))}
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
            <title>Server Logs</title>
            {CSS}
        </head>

        <body>

        <div class="container">

            <div class="card">

                <h1 class="brand">
                    Server Logs
                </h1>

                <nav>
                    <a href="/">Dashboard</a>
                    <a href="/configuration">Configurations</a>
                    <a href="/users">User Controls</a>
                    <a href="/logout">Logout</a>
                </nav>

            </div>

            <div class="card">

                <h2>Execution Logs</h2>

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

@app.get("/configuration", response_class=HTMLResponse)
def configuration_page(request: Request):

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

    checked = "checked" if use_cc else ""

    return HTMLResponse(
        f"""
        <!DOCTYPE html>

        <html>

        <head>
            <title>Configurations</title>
            {CSS}
        </head>

        <body>

        <div class="container">

            <div class="card">

                <h1 class="brand">
                    Configurations
                </h1>

                <nav>
                    <a href="/">Dashboard</a>
                    <a href="/logs">Server Logs</a>
                    <a href="/users">User Controls</a>
                    <a href="/logout">Logout</a>
                </nav>

            </div>

            <div class="card">

                <h2>API Parameters</h2>

                <form
                    method="post"
                    action="/configuration"
                >

                    <label>
                        Google Gemini API Token Key
                    </label>

                    <input
                        type="password"
                        name="gemini_api_key"
                        value="{html.escape(gemini_key)}"
                    >

                    <label>

                        <input
                            type="checkbox"
                            name="use_cloudconvert"
                            {checked}
                            style="width:auto;"
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
                    >

                    <button
                        type="submit"
                        class="btn-green"
                    >
                        Save Parameters
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
        "True" if use_cloudconvert else "False"
    )

    return RedirectResponse(
        "/configuration",
        status_code=303
    )


# ============================================================
# USER MANAGEMENT
# ============================================================

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):

    redirect = require_login(request)

    if redirect:
        return redirect

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT username FROM users ORDER BY username"
    )

    users = cursor.fetchall()
    conn.close()

    users_html = "".join(
        f"<li>{html.escape(user[0])}</li>"
        for user in users
    )

    return HTMLResponse(
        f"""
        <!DOCTYPE html>

        <html>

        <head>
            <title>User Controls</title>
            {CSS}
        </head>

        <body>

        <div class="container">

            <div class="card">

                <h1 class="brand">
                    User Controls
                </h1>

                <nav>
                    <a href="/">Dashboard</a>
                    <a href="/logs">Server Logs</a>
                    <a href="/configuration">
                        Configurations
                    </a>
                    <a href="/logout">Logout</a>
                </nav>

            </div>

            <div class="grid">

                <div class="card">

                    <h2>Register New User</h2>

                    <form
                        method="post"
                        action="/users"
                    >

                        <label>New Username ID</label>

                        <input
                            name="username"
                            required
                        >

                        <label>New Password</label>

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

                    <h2>Registered Users</h2>

                    <ul>
                        {users_html}
                    </ul>

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
            (username, hashed)
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
                        Error: Username already exists.
                    </h2>

                    <a class="btn" href="/users">
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
