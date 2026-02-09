import streamlit as st
import requests
import sqlite3
import pandas as pd
from datetime import datetime
import uuid
import re
import json
from typing import Optional, List, Dict, Any

# --- Constants & Configuration ---
DEFAULT_API_URL = "http://203.185.144.34:8080/v1"
DEFAULT_API_KEY = "app-4x1vEGpp9Adha7vc4msobuQk"
DB_FILE = "chatbot.db"

# ตั้งค่าหน้าเว็บ (ตั้่งค่าเป็นอันดับแรกสุดของไฟล์)
st.set_page_config(
    page_title="ระบบผู้เชี่ยวชาญศาลปกครอง AI (OAC Expert)",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Class: Database Manager ---
class DatabaseManager:
    """จัดการการเชื่อมต่อและคำสั่ง SQL กับฐานข้อมูล SQLite"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """สร้างตารางข้อมูลถ้ายังไม่มี และอัปเดต Schema อัตโนมัติ"""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    user_question TEXT,
                    ai_response TEXT,
                    rating INTEGER,
                    feedback_comment TEXT,
                    session_id TEXT
                )
            ''')
            
            # --- Migration Logic for Existing Database ---
            c.execute("PRAGMA table_info(chat_logs)")
            existing_columns = [col[1] for col in c.fetchall()]
            
            if 'session_id' not in existing_columns:
                c.execute("ALTER TABLE chat_logs ADD COLUMN session_id TEXT")
                
            if 'feedback_comment' not in existing_columns:
                 c.execute("ALTER TABLE chat_logs ADD COLUMN feedback_comment TEXT")
                 
            conn.commit()

    def save_chat_log(self, question: str, answer: str, session_id: str) -> int:
        """บันทึกบทสนทนาและคืนค่า ID ของแถวที่บันทึก"""
        with self._get_connection() as conn:
            c = conn.cursor()
            timestamp = datetime.now()
            c.execute(
                "INSERT INTO chat_logs (timestamp, user_question, ai_response, rating, feedback_comment, session_id) VALUES (?, ?, ?, ?, ?, ?)",
                (timestamp, question, answer, None, None, session_id)
            )
            return c.lastrowid

    def update_rating(self, log_id: int, rating: int, comment: Optional[str] = None):
        """อัปเดตคะแนนความพึงพอใจ"""
        with self._get_connection() as conn:
            c = conn.cursor()
            query = "UPDATE chat_logs SET rating = ?"
            params = [rating]
            
            if comment:
                query += ", feedback_comment = ?"
                params.append(comment)
                
            query += " WHERE id = ?"
            params.append(log_id)
            
            c.execute(query, tuple(params))
            conn.commit()

    def load_logs(self) -> pd.DataFrame:
        """ดึงข้อมูลทั้งหมดมาแสดงเป็น DataFrame"""
        with self._get_connection() as conn:
            df = pd.read_sql_query("SELECT * FROM chat_logs ORDER BY timestamp DESC", conn)
        return df

# --- Class: Dify API Client ---
class DifyClient:
    """จัดการการเชื่อมต่อกับ Dify AI API"""
    
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

    def chat_stream(self, user_message: str, user_id: str, conversation_id: str = ""):
        """ส่งข้อความแบบ Streaming และคืนค่า Generator"""
        if not self.api_key:
            yield {"type": "error", "content": "ไม่พบ API Key"}
            return

        url = f"{self.api_url}/chat-messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "inputs": {},
            "query": user_message,
            "response_mode": "streaming",
            "user": user_id,
            "conversation_id": conversation_id
        }

        try:
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=60) as response:
                if response.status_code == 401:
                    yield {"type": "error", "content": "⛔ Error 401: Unauthorized API Key"}
                    return
                
                response.raise_for_status()
                
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith('data: '):
                            json_str = line[6:]
                            try:
                                data = json.loads(json_str)
                                event = data.get('event')
                                
                                if event == 'message':
                                    yield {"type": "text", "content": data.get('answer', '')}
                                elif event == 'message_end':
                                    yield {
                                        "type": "end", 
                                        "conversation_id": data.get('conversation_id'),
                                        "message_id": data.get('message_id'),
                                        "metadata": data.get('metadata', {})
                                    }
                                elif event == 'error':
                                    yield {"type": "error", "content": data.get('message')}
                            except:
                                pass
        except Exception as e:
            yield {"type": "error", "content": f"Connection Error: {str(e)}"}

    def get_suggestions(self, message_id: str, user_id: str) -> List[str]:
        """ดึงคำถามแนะนำจาก AI"""
        if not message_id:
            return []
            
        url = f"{self.api_url}/messages/{message_id}/suggested"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        params = {"user": user_id}
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("data", [])
            return []
        except:
            return []

    def _clean_think_tags(self, text: str) -> str:
        """ลบข้อความภายใน tag <think>...</think> ออก"""
        if not text:
            return ""
        # Remove complete tags
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        # Remove incomplete/open tags (optional, for streaming display)
        text = re.sub(r'<think>.*', '', text, flags=re.DOTALL) 
        return text.strip()

# --- Class: User Interface (The App) ---
class AdministrativeCourtApp:
    """จัดการหน้าจอและการแสดงผลของ Application"""
    
    def __init__(self):
        self.db = DatabaseManager(DB_FILE)
        self.api_client = None # จะ initialize หลังจากได้ API Key
        
        # Initialize Session State
        if "messages" not in st.session_state:
            st.session_state.messages = []
        if "conversation_id" not in st.session_state:
            st.session_state.conversation_id = ""
        if "user_id" not in st.session_state:
            st.session_state.user_id = f"user-{uuid.uuid4().hex[:8]}"
        if "last_log_id" not in st.session_state:
            st.session_state.last_log_id = None
        if "current_suggestions" not in st.session_state:
            st.session_state.current_suggestions = []

    def load_css(self):
        """โหลด CSS ปรับแต่งความสวยงาม"""
        st.markdown("""
            <style>
            @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@300;400;500;700&display=swap');
            html, body, [class*="css"] { font-family: 'Sarabun', sans-serif; }
            
            :root {
                --primary: #002855; /* Navy Blue */
                --secondary: #C5A059; /* Gold */
                --bg-light: #F4F6F9;
            }
            .stApp { background-color: var(--bg-light); }
            
            /* Header */
            h1 { color: var(--primary); font-weight: 700; border-bottom: 2px solid var(--secondary); padding-bottom: 10px; }
            h2, h3 { color: var(--primary); }
            
            /* Chat Bubbles */
            .stChatMessage { background-color: white; border-radius: 15px; padding: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
            .stChatMessage[data-testid="stChatMessageUser"] { background-color: #E3F2FD; border-left: 5px solid var(--primary); }
            .stChatMessage[data-testid="stChatMessageAssistant"] { background-color: #FFF8E1; border-left: 5px solid var(--secondary); }
            
            /* Button Override */
            .stButton button { width: 100%; border-radius: 8px; }
            </style>
        """, unsafe_allow_html=True)

    def _get_court_logo(self) -> str:
        """คืนค่า Base64 Data URI ของโลโก้ศาลปกครอง"""
        import base64
        import os
        
        logo_path = "court_logo.png"
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                data = f.read()
                b64 = base64.b64encode(data).decode('utf-8')
                return f"data:image/png;base64,{b64}"
        
        # Fallback SVG Icon if image not found
        svg = """
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#C5A059">
          <path d="M12 2C13.1 2 14 2.9 14 4V5H20C21.1 5 22 5.9 22 7V9H20V12C20 14.21 18.21 16 16 16C13.79 16 12 14.21 12 12V9H14V7H10V9H12V12C12 14.21 10.21 16 8 16C5.79 16 4 14.21 4 12V9H2V7C2 5.9 2.9 5 4 5H10V4C10 2.9 10.9 2 12 2ZM6 7V9H8V7H6ZM16 7V9H18V7H16Z"/>
          <path d="M12 18C14.7 18 17.2 19.1 19 20.9L17.6 22.3C16.1 21.1 14.1 20 12 20C9.9 20 7.9 21.1 6.4 22.3L5 20.9C6.8 19.1 9.3 18 12 18Z"/>
        </svg>
        """
        b64 = base64.b64encode(svg.encode('utf-8')).decode('utf-8')
        return f"data:image/svg+xml;base64,{b64}"

    def render_sidebar(self):
        """แสดงผล Sidebar พร้อมโลโก้และเมนู (Systematic Layout)"""
        
        # --- Zone 1: Identity (อัตลักษณ์องค์กร) ---
        st.sidebar.markdown(
            f"""
            <div style="text-align: center; padding: 0px 0 10px 0;">
                <img src="{self._get_court_logo()}" width="110" style="margin-bottom: 10px; border-radius: 50%;">
                <h2 style="color: #002855; margin: 0; font-size: 1.4rem; font-weight: 700; font-family: 'Sarabun';">ศาลปกครอง</h2>
                <p style="color: #555; font-size: 0.9rem; margin-top: 5px; font-weight: 400;">Administrative Court</p>
            </div>
            """, 
            unsafe_allow_html=True
        )
        
        st.sidebar.markdown("---")

        # --- Zone 2: Control (เมนูคำสั่ง) ---
        st.sidebar.caption("MENU")
        
        # 2.1 Start New Chat
        if st.sidebar.button("➕ เริ่มการสนทนาใหม่", type="primary", use_container_width=True):
            st.session_state.messages = []
            st.session_state.conversation_id = ""
            st.session_state.last_log_id = None
            st.session_state.current_suggestions = []
            st.session_state.user_id = f"user-{uuid.uuid4().hex[:8]}" 
            st.rerun()
            
        # 2.2 Navigation (Close gap)
        menu_options = {
            "💬 ปรึกษาคดี (Public)": "public",
            "🔒 ผู้ดูแลระบบ (Admin)": "admin"
        }
        selection = st.sidebar.radio("เลือกหน้าข", list(menu_options.keys()), label_visibility="collapsed")
        
        # --- Zone 3: Footer (เครดิตโครงการ) ---
        # Push to bottom slightly
        st.sidebar.markdown("<br><br>", unsafe_allow_html=True)
        st.sidebar.markdown("---")
        
        st.sidebar.markdown(
            """
            <div style="text-align: center; font-size: 0.8rem; color: #666; line-height: 1.4;">
                <span style="font-weight: 300;">ผลงานภายใต้</span><br>
                <b style="color: #002855;">Bootcamp: LLM Research<br>Challenge Thailand 2026</b>
            </div>
            """,
            unsafe_allow_html=True
        )
        
        try:
            st.sidebar.image("sponsors.png", use_container_width=True)
        except:
            pass
        
        # Initialize API Client (Fixed Key)
        self.api_client = DifyClient(DEFAULT_API_URL, DEFAULT_API_KEY)
        
        return selection

    def render_chat_page(self):
        """หน้าจอแชทสำหรับประชาชน"""
        # Hero Section (แสดงเฉพาะตอนเริ่มแชทใหม่ๆ)
        if not st.session_state.messages:
            st.markdown("""
                <div style="text-align: center; padding: 2rem 0; background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); border-radius: 10px; margin-bottom: 2rem;">
                    <h1 style="color: #002855; border: none; margin-bottom: 0.5rem;">⚖️ ระบบผู้เชี่ยวชาญศาลปกครอง AI</h1>
                    <p style="color: #444; font-size: 1.1rem;">ผู้ช่วยอัจฉริยะในการให้คำปรึกษาขั้นตอนการฟ้องคดีและระเบียบปฏิบัติ</p>
                </div>
            """, unsafe_allow_html=True)
            
            # Suggestion Chips
            col1, col2, col3 = st.columns(3)
            if col1.button("ขั้นตอนการฟ้องคดี 📝"):
                self._handle_click_suggestion("ขั้นตอนการฟ้องคดีปกครองมีอะไรบ้าง?")
            if col2.button("ระยะเวลาการฟ้อง ⏳"):
                self._handle_click_suggestion("ระยะเวลาในการฟ้องคดีปกครองคือกี่วัน?")
            if col3.button("ค่าธรรมเนียมศาล 💰"):
                self._handle_click_suggestion("ค่าธรรมเนียมศาลปกครองคิดอย่างไร?")

            st.info("⚠️ คำเตือน: คำตอบจาก AI เป็นเพียงข้อมูลเบื้องต้นเพื่ออำนวยความสะดวก ไม่สามารถใช้อ้างอิงทางกฎหมายได้")

        # Display History
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                # Show Thought Process if available
                if msg.get("thought"):
                    with st.expander(f"🧠 กระบวนการคิดวิเคราะห์ ({len(msg['thought'])} chars)"):
                        st.markdown(msg["thought"])
                        
                st.markdown(msg["content"])
                
                # Check for Citations in history
                if msg.get("citations"):
                    with st.expander("📚 เอกสารอ้างอิง"):
                        for idx, cit in enumerate(msg["citations"]):
                            score = cit.get('score', 0)
                            if score > 0.4:
                                doc_name = cit.get('document_name', 'เอกสารประกอบ')
                                content = cit.get('content', '')[:200]
                                st.markdown(f"**{idx+1}. {doc_name}** ({score:.0%})")
                                st.caption(f"_{content}..._")
                
    def _generate_response(self, prompt: str):
        """ฟังก์ชันกลางสำหรับส่งข้อความหา AI และจัดการการแสดงผลแบบ Streaming พร้อม Thought Process"""
        # 1. แสดงและบันทึกข้อความผู้ใช้
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # 2. AI ประมวลผล (Streaming with Thought)
        full_response = ""
        full_thought = ""
        citations = []
        conversation_id = st.session_state.conversation_id
        message_id = ""
        
        with st.chat_message("assistant"):
            # Prepare containers
            thought_expander = st.expander("🧠 กำลังวิเคราะห์ข้อกฎหมาย...", expanded=True)
            thought_placeholder = thought_expander.empty()
            response_placeholder = st.empty()
            
            is_thinking = False
            
            # Streaming Loop (แอบสั่งให้คิดเป็นภาษาไทย)
            system_instruction = " (กรุณาคิดวิเคราะห์และตอบเป็นภาษาไทย)"
            stream = self.api_client.chat_stream(prompt + system_instruction, st.session_state.user_id, st.session_state.conversation_id)
            for chunk in stream:
                if chunk["type"] == "text":
                    content = chunk["content"]
                    
                    # Simple State Machine for <think> tag
                    if "<think>" in content:
                        is_thinking = True
                        content = content.replace("<think>", "")
                    
                    if "</think>" in content:
                        is_thinking = False
                        parts = content.split("</think>")
                        full_thought += parts[0]
                        full_response += parts[1]
                        
                        # Update UI one last time for thought, then collapse
                        thought_placeholder.markdown(full_thought)
                        # thought_expander.update(label="🧠 วิเคราะห์เสร็จสิ้น", expanded=False) # Helper text
                        response_placeholder.markdown(full_response + "▌")
                        continue

                    if is_thinking:
                        full_thought += content
                        thought_placeholder.markdown(full_thought + "▌")
                    else:
                        full_response += content
                        response_placeholder.markdown(full_response + "▌")
                    
                elif chunk["type"] == "end":
                    conversation_id = chunk["conversation_id"]
                    message_id = chunk["message_id"]
                    meta = chunk.get("metadata", {})
                    citations = meta.get("retriever_resources", [])
                    
                elif chunk["type"] == "error":
                    st.error(chunk["content"])
                    return

            # Final Cleanup
            thought_placeholder.markdown(full_thought)
            response_placeholder.markdown(full_response)
            
            # Display Citations (Immediate view)
            if citations:
                with st.expander("📚 เอกสารอ้างอิง"):
                    for idx, cit in enumerate(citations):
                        score = cit.get('score', 0)
                        if score > 0.4: # Only show relevant docs
                             doc_name = cit.get('document_name', 'เอกสารประกอบ')
                             content = cit.get('content', '')[:200]
                             st.markdown(f"**{idx+1}. {doc_name}** ({score:.0%})")
                             st.caption(f"_{content}..._")
        
        # 3. บันทึกข้อความตอบกลับและ Log (รวม Citations & Thought)
        st.session_state.messages.append({
            "role": "assistant", 
            "content": full_response,
            "thought": full_thought,
            "citations": citations
        })
        
        # Save Log
        log_id = self.db.save_chat_log(prompt, full_response, st.session_state.user_id)
        st.session_state.last_log_id = log_id
        st.session_state.conversation_id = conversation_id
        
        # 4. Fetch Suggestions
        st.session_state.current_suggestions = self.api_client.get_suggestions(message_id, st.session_state.user_id)
        
        st.rerun()

    def _handle_click_suggestion(self, prompt):
        """จัดการเมื่อผู้ใช้กดปุ่มแนะนำ -> เรียก Flow เดียวกับการพิมพ์"""
        self._generate_response(prompt)

    def handle_chat_input(self):
        """รอรับ Input จากช่องแชท"""
        if prompt := st.chat_input("พิมพ์คำถามของคุณที่นี่..."):
            self._generate_response(prompt)

        # Feedback Section
        if st.session_state.last_log_id:
            self.render_feedback_section()

    def render_feedback_section(self):
        """ส่วนแสดงผลการให้คะแนน (Minimal Style) + คำถามแนะนำ"""
        # 1. Feedback
        c_label, c1, c2, c3, c4, c5, c_void = st.columns([1.5, 0.7, 0.7, 0.7, 0.7, 0.7, 4])
        with c_label:
            st.caption("พึงพอใจคำตอบนี้ไหม? 👉")
            
        def _rate(score):
            self.db.update_rating(st.session_state.last_log_id, score)
            st.toast(f"ขอบคุณสำหรับคะแนน {score} ดาวครับ! 🙏")
            # Clear id to hide inputs but Keep suggestions
            st.session_state.last_log_id = None 
            st.rerun()
            
        if c1.button("1⭐", key="r1", help="ต้องปรับปรุง"): _rate(1)
        if c2.button("2⭐", key="r2", help="พอใช้"): _rate(2)
        if c3.button("3⭐", key="r3", help="ปานกลาง"): _rate(3)
        if c4.button("4⭐", key="r4", help="ดี"): _rate(4)
        if c5.button("5⭐", key="r5", help="ดีเยี่ยม"): _rate(5)

        # 2. Suggested Questions
        if st.session_state.current_suggestions:
            st.markdown("---")
            st.caption("💡 คำถามที่เกี่ยวข้อง:")
            
            # Limit to 3 suggestions for better UI
            suggestions = st.session_state.current_suggestions[:3]
            cols = st.columns(len(suggestions))
            
            for idx, q in enumerate(suggestions):
                # Use container width for better mobile touch targets
                if cols[idx].button(q, key=f"sugg_{idx}", use_container_width=True):
                    self._handle_click_suggestion(q)

    def render_admin_page(self):
        """หน้าจอ Dashboard สำหรับ Admin"""
        st.title("🔒 Admin Dashboard")

        # Login Logic
        if "is_admin" not in st.session_state:
            st.session_state.is_admin = False

        if not st.session_state.is_admin:
            pwd = st.text_input("Admin Password", type="password")
            if st.button("Login"):
                if pwd == "admin":
                    st.session_state.is_admin = True
                    st.rerun()
                else:
                    st.error("รหัสผ่านผิดพลาด")
            return

        # Dashboard Logic
        if st.button("Logout", type="primary"):
            st.session_state.is_admin = False
            st.rerun()
            
        df = self.db.load_logs()
        
        # Metrics
        col1, col2 = st.columns(2)
        col1.metric("จำนวนบทสนทนาทั้งหมด", len(df))
        avg_rating = df['rating'].mean()
        col2.metric("คะแนนความพึงพอใจเฉลี่ย", f"{avg_rating:.2f} ⭐" if not pd.isna(avg_rating) else "-")

        # Chart
        st.subheader("📊 แนวโน้มการใช้งาน")
        if not df.empty:
            df['date'] = pd.to_datetime(df['timestamp']).dt.date
            daily_stats = df['date'].value_counts().sort_index()
            st.bar_chart(daily_stats)

        # Table
        st.subheader("📝 บันทึกข้อมูล (Logs)")
        st.dataframe(df, use_container_width=True)
        
        # Export
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            "📥 Download Log (CSV)",
            df.to_csv(index=False).encode('utf-8-sig'),
            f"chat_logs_{timestamp_str}.csv",
            "text/csv"
        )

# --- Main Entry Point ---
def main():
    app = AdministrativeCourtApp()
    app.load_css()
    
    current_page = app.render_sidebar()
    
    if current_page == "💬 ปรึกษาคดี (Public)":
        app.render_chat_page()
        app.handle_chat_input()
    else:
        app.render_admin_page()

if __name__ == "__main__":
    main()
