import flet as ft
import os
import time
import sqlite3
import threading
import bcrypt
import requests
from gtts import gTTS
from pydub import AudioSegment
import google.generativeai as genai
import cloudconvert

# =====================================================================
# DATA HARDENING LAYER (SQLITE SINGLE FILE COMPACT STORAGE)
# =====================================================================
DB_DIR = "instance"
DB_PATH = os.path.join(DB_DIR, "data.db")
STORAGE_DIR = os.path.join("storage", "outputs")

os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)

def database_provisioner():
    conn = sqlite3.connect(DB_PATH)
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
    
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        hashed_bytes = bcrypt.hashpw("smbagathi".encode('utf-8'), bcrypt.gensalt())
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", hashed_bytes.decode('utf-8')))
        
    conn.commit()
    conn.close()

def pull_config(key: str, default_val: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM configs WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default_val

def push_config(key: str, value_str: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO configs (key, value) VALUES (?, ?)", (key, value_str))
    conn.commit()
    conn.close()

# =====================================================================
# AUTOMATION ENGINE PIPELINE CORE (REAL VOICE SYNTHESIS & ENCODING)
# =====================================================================
class ProductionPodcastEngine:
    def __init__(self):
        self.gemini_key = pull_config("gemini_api_key", "")
        self.use_cc = pull_config("use_cloudconvert", "False") == "True"
        self.cc_key = pull_config("cloudconvert_api_key", "")
        
        if self.gemini_key:
            genai.configure(api_key=self.gemini_key)

    def execute_workflow_pipeline(self, raw_text: str, operator_name: str, status_callback) -> str:
        temp_raw_audio = os.path.join(STORAGE_DIR, f"raw_{int(time.time())}.mp3")
        final_processed_audio = os.path.join(STORAGE_DIR, f"podcast_{int(time.time())}.mp3")
        
        try:
            if not self.gemini_key:
                raise Exception("Google Gemini Authorization API key is blank.")

            status_callback("Gemini AI is parsing and rewriting text to conversational format...", 0.20)
            model = genai.GenerativeModel("gemini-1.5-flash")
            refining_prompt = (
                "Rewrite the following article text into a highly polished, engaging, natural narrative script "
                "suitable for a single-voice spoken podcast episode. Clean up links, timestamps, and WhatsApp artifacts:\n\n"
                f"{raw_text}"
            )
            response = model.generate_content(refining_prompt)
            podcast_script = response.text

            status_callback("Synthesizing audio channels via direct Google voice infrastructure...", 0.45)
            tts_client = gTTS(text=podcast_script, lang='en', slow=False)
            tts_client.save(temp_raw_audio)

            if self.use_cc:
                if not self.cc_key:
                    raise Exception("CloudConvert selected but your API Token Key is blank.")
                status_callback("Provisioning CloudConvert Cloud Node Transcoder worker...", 0.65)
                
                api_client = cloudconvert.Api(api_key=self.cc_key)
                
                job_payload = api_client.Job.create(payload={
                    "tasks": {
                        "upload-source-media": {"operation": "import/upload"},
                        "ffmpeg-transcode-task": {
                            "operation": "convert",
                            "input": "upload-source-media",
                            "output_format": "mp3",
                            "engine": "ffmpeg",
                            "audio_codec": "libmp3lame",
                            "audio_bitrate": 64,       
                            "audio_channels": 1        
                        },
                        "export-final-media": {"operation": "export/url", "input": "ffmpeg-transcode-task"}
                    }
                })
                
                upload_task = api_client.Task.find(id=job_payload['tasks']['id'])
                api_client.Task.upload(file_name=temp_raw_audio, task=upload_task)
                
                status_callback("Executing Cloud compression task (-b:a 64k -ac 1)...", 0.85)
                completed_job = api_client.Job.wait(id=job_payload['id'])
                
                export_task = [t for t in completed_job['tasks'] if t['name'] == 'export-final-media']
                download_url = export_task['result']['files']['url']
                
                media_bytes = requests.get(download_url).content
                with open(final_processed_audio, "wb") as file_out:
                    file_out.write(media_bytes)
            else:
                status_callback("Executing local direct transcode via server FFmpeg pipeline...", 0.70)
                native_audio_segment = AudioSegment.from_mp3(temp_raw_audio)
                
                status_callback("Downsampling bitstreams natively to 64kbps mono format...", 0.85)
                native_audio_segment.export(
                    final_processed_audio,
                    format="mp3",
                    bitrate="64k",
                    parameters=["-ac", "1", "-c:a", "libmp3lame"]
                )

            if os.path.exists(temp_raw_audio):
                os.remove(temp_raw_audio)

            status_callback("Automation sequence successful. Audio file archived on disk.", 1.0)
            return final_processed_audio

        except Exception as error_fault:
            if os.path.exists(temp_raw_audio):
                os.remove(temp_raw_audio)
            status_callback(f"Pipeline Halt Error: {str(error_fault)}", 0.0)
            return ""

# =====================================================================
# GRAPHICAL APP INTERFACE CORE LAYOUT (WEB, DESKTOP, AND MOBILE)
# =====================================================================
def main(page: ft.Page):
    page.title = "gs_podcast_automation"
    page.theme_mode = ft.ThemeMode.DARK
    page.primary_color = ft.colors.BLUE_400
    page.scroll = ft.ScrollMode.AUTO
    
    logged_in_user = None

    user_input_box = ft.TextField(label="System Operator Username", width=300)
    pass_input_box = ft.TextField(label="System Password Token Key", password=True, can_reveal_password=True, width=300)
    login_error_lbl = ft.Text(color=ft.colors.RED_400)

    def authentication_trigger(e):
        nonlocal logged_in_user
        username = user_input_box.value.strip()
        password = pass_input_box.value
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        row = c.fetchone()
        conn.close()
        
        if row and bcrypt.checkpw(password.encode('utf-8'), row[0].encode('utf-8')):
            logged_in_user = username
            login_error_lbl.value = ""
            render_authenticated_application_dashboard()
        else:
            login_error_lbl.value = "Authentication Failure: Bad user credential tokens."
            page.update()

    def display_login_form_view():
        page.clean()
        page.add(
            ft.Container(
                content=ft.Column([
                    ft.Icon(ft.icons.ROUTING_ROUNDED, size=60, color=ft.colors.BLUE_400),
                    ft.Text("gs_podcast_automation Secure Gateway", size=20, weight=ft.FontWeight.BOLD),
                    user_input_box,
                    pass_input_box,
                    ft.VerticalDivider(height=15),
                    ft.ElevatedButton("Login & Open Workspace", on_click=authentication_trigger, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=45),
                    login_error_lbl
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                alignment=ft.alignment.center,
                padding=40,
                expand=True
            )
        )
        page.update()

    def render_authenticated_application_dashboard():
        page.clean()
        
        article_input = ft.TextField(label="WhatsApp Content/Article Input Buffer", multiline=True, min_lines=6, max_lines=12, hint_text="Paste your morning message feeds right here...")
        telemetry_lbl = ft.Text("System Pipeline Engine Status: Awaiting Job Stream Entry...", italic=True, color=ft.colors.GREY_400)
        task_progress_bar = ft.ProgressBar(value=0, width=500, color=ft.colors.BLUE_400)
        
        gemini_token_box = ft.TextField(label="Google Gemini Developer API Token Key", password=True, can_reveal_password=True, value=pull_config("gemini_api_key"))
        cloudconvert_toggle = ft.Switch(label="Route Compression Logic via External CloudConvert APIs", value=pull_config("use_cloudconvert", "False") == "True")


# =====================================================================
# SERVER STARTUP FOR RENDER
# =====================================================================
if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, port=port, host="0.0.0.0")
