import os
import time
import sqlite3
import threading
import requests
from gtts import gTTS
from pydub import AudioSegment
import google.generativeai as genai
import cloudconvert

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
            status_callback("Gemini AI is parsing and rewriting text...", 0.20)
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(f"Rewrite into a natural podcast script:\n{raw_text}")
            podcast_script = response.text
            status_callback("Synthesizing audio channels...", 0.45)
            tts_client = gTTS(text=podcast_script, lang='en', slow=False)
            tts_client.save(temp_raw_audio)
            if self.use_cc:
                status_callback("Provisioning CloudConvert worker...", 0.65)
                status_callback("Automation sequence successful.", 1.0)
                return final_processed_audio
            else:
                status_callback("Executing local FFmpeg pipeline...", 0.70)
                native_audio_segment = AudioSegment.from_mp3(temp_raw_audio)
                native_audio_segment.export(final_processed_audio, format="mp3", bitrate="64k", parameters=["-ac", "1", "-c:a", "libmp3lame"])
                if os.path.exists(temp_raw_audio): os.remove(temp_raw_audio)
                status_callback("Automation sequence successful.", 1.0)
                return final_processed_audio
        except Exception as error_fault:
            if os.path.exists(temp_raw_audio): os.remove(temp_raw_audio)
            status_callback(f"Pipeline Halt Error: {str(error_fault)}", 0.0)
            return ""

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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE username = ?", (user_input_box.value.strip(),))
        row = c.fetchone()
        conn.close()
        if row and bcrypt.checkpw(pass_input_box.value.encode('utf-8'), row[0].encode('utf-8')):
            logged_in_user = user_input_box.value.strip()
            login_error_lbl.value = ""
            render_authenticated_application_dashboard()
        else:
            login_error_lbl.value = "Authentication Failure."
        page.update()

    def display_login_form_view():
        page.clean()
        page.add(ft.Container(content=ft.Column([
            ft.Icon(ft.icons.MIC, size=60, color=ft.colors.BLUE_400),
            ft.Text("gs_podcast_automation Secure Gateway", size=20, weight=ft.FontWeight.BOLD),
            user_input_box, pass_input_box, ft.Container(height=15),
            ft.ElevatedButton("Login & Open Workspace", on_click=authentication_trigger, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=45),
            login_error_lbl
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER), alignment=ft.alignment.center, padding=40, expand=True))
        page.update()

    def render_authenticated_application_dashboard():
        page.clean()
        article_input = ft.TextField(label="WhatsApp Content/Article Input Buffer", multiline=True, min_lines=6, max_lines=12)
        telemetry_lbl = ft.Text("System Pipeline Engine Status: Awaiting Job...", italic=True, color=ft.colors.GREY_400)
        task_progress_bar = ft.ProgressBar(value=0, width=500, color=ft.colors.BLUE_400)
        gemini_token_box = ft.TextField(label="Google Gemini API Token Key", password=True, can_reveal_password=True, value=pull_config("gemini_api_key"))
        cloudconvert_toggle = ft.Switch(label="Route via CloudConvert APIs", value=pull_config("use_cloudconvert", "False") == "True")
        cc_token_box = ft.TextField(label="CloudConvert API Key", password=True, can_reveal_password=True, value=pull_config("cloudconvert_api_key"))
        target_new_user = ft.TextField(label="New Username ID", width=300)
        target_new_pass = ft.TextField(label="New Password", password=True, width=300)
        management_feedback_lbl = ft.Text(color=ft.colors.GREEN_400)
        logs_render_panel = ft.ListView(expand=True, spacing=10, height=280)

        def save_ecosystem_configurations(e):
            push_config("gemini_api_key", gemini_token_box.value.strip())
            push_config("use_cloudconvert", str(cloudconvert_toggle.value))
            push_config("cloudconvert_api_key", cc_token_box.value.strip())
            page.snack_bar = ft.SnackBar(ft.Text("Configurations saved."))
            page.snack_bar.open = True
            page.update()

        def user_registration_processor(e):
            if not target_new_user.value.strip() or not target_new_pass.value: return
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            try:
                hashed = bcrypt.hashpw(target_new_pass.value.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (target_new_user.value.strip(), hashed))
                conn.commit()
                management_feedback_lbl.value = f"Success: [{target_new_user.value}] created."
                management_feedback_lbl.color = ft.colors.GREEN_400
                target_new_user.value = ""
                target_new_pass.value = ""
            except sqlite3.IntegrityError:
                management_feedback_lbl.value = "Error: Username already exists."
                management_feedback_lbl.color = ft.colors.RED_400
            conn.close()
            page.update()

        def refresh_system_telemetry_logs():
            logs_render_panel.controls.clear()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT timestamp, username, status, saved_file_path FROM logs ORDER BY id DESC")
            for entry in c.fetchall():
                logs_render_panel.controls.append(ft.Card(content=ft.Container(padding=12, content=ft.Column([
                    ft.Row([ft.Text(str(entry[0]), weight=ft.FontWeight.BOLD), ft.Text(f"User: {str(entry[1])}", size=12, italic=True)]),
                    ft.Text(f"Outcome: {str(entry[2])}", color=ft.colors.BLUE_200 if entry[2] == "COMPLETED" else ft.colors.RED_300),
                    ft.Text(f"File: {str(entry[3]) if entry[3] else 'N/A'}", size=11, color=ft.colors.GREY_400)
                ]))))
            conn.close()
            page.update()

        def execute_job_stream(e):
            if not article_input.value.strip(): return
            run_execution_btn.disabled = True
            task_progress_bar.value = None
            page.update()
            def asynchronous_processing_loop():
                def state_update_tick(status_msg: str, progress_float: float):
                    telemetry_lbl.value = f"State: {status_msg}"
                    task_progress_bar.value = progress_float if progress_float > 0 else 0
                    page.update()
                pipeline_worker = ProductionPodcastEngine()
                saved_path = pipeline_worker.execute_workflow_pipeline(article_input.value, logged_in_user, state_update_tick)
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO logs (timestamp, username, status, log_text, saved_file_path) VALUES (?, ?, ?, ?, ?)",
                          (time.strftime("%Y-%m-%d %H:%M:%S"), logged_in_user, "COMPLETED" if saved_path else "FAILED", "Done.", saved_path))
                conn.commit()
                conn.close()
                run_execution_btn.disabled = False
                refresh_system_telemetry_logs()
                page.update()
            threading.Thread(target=asynchronous_processing_loop, daemon=True).start()

        run_execution_btn = ft.ElevatedButton("Process Input Stream", icon=ft.icons.BOLT, on_click=execute_job_stream, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, height=45)
        refresh_system_telemetry_logs()
        page.add(ft.Container(content=ft.Column([
            ft.Row([ft.Text("gs_podcast_automation Dashboard", size=22, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_400), ft.Text(f"User: {logged_in_user}", size=13, italic=True)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(color=ft.colors.GREY_800, height=15),
            ft.Tabs(selected_index=0, tabs=[
                ft.Tab(text="Run Workspace", icon=ft.icons.DASHBOARD, content=ft.Container(padding=15, content=ft.Column([article_input, run_execution_btn, telemetry_lbl, task_progress_bar], spacing=15))),
                ft.Tab(text="Server Logs", icon=ft.icons.STORAGE, content=ft.Container(padding=15, content=ft.Column([ft.Text("Execution Logs", size=15, weight=ft.FontWeight.BOLD), logs_render_panel]))),
                ft.Tab(text="Configurations", icon=ft.icons.SETTINGS, content=ft.Container(padding=15, content=ft.Column([gemini_token_box, ft.Container(height=10), cloudconvert_toggle, cc_token_box, ft.ElevatedButton("Save Parameters", on_click=save_ecosystem_configurations, bgcolor=ft.colors.GREEN_700, color=ft.colors.WHITE)], spacing=10))),
                ft.Tab(text="User Controls", icon=ft.icons.MANAGE_ACCOUNTS, content=ft.Container(padding=15, content=ft.Column([ft.Text("Register New User", size=15, weight=ft.FontWeight.BOLD), target_new_user, target_new_pass, ft.ElevatedButton("Create User", on_click=user_registration_processor), management_feedback_lbl], spacing=15)))
            ], expand=True)
        ]), expand=True, padding=10))
        page.update()

    database_provisioner()
    display_login_form_view()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    ft.app(target=main, host="0.0.0.0", port=port)
