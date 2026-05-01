import streamlit as st
import anthropic
import base64
import json
import re
import csv
import io
import requests
from datetime import datetime, date

st.set_page_config(
    page_title="מזהה קלוריות חכם",
    page_icon="🍽️",
    layout="centered"
)

st.markdown("""
<style>
    html, body, [class*="css"] {
        direction: rtl;
        font-family: 'Segoe UI', sans-serif;
    }
    .block-container {
        max-width: 760px;
        margin: 0 auto !important;
        padding: 2rem 1rem;
    }
    h1, h2, h3, h4, h5 { text-align: center !important; }
    p, label, .stCaption, [data-testid="stCaptionContainer"] p,
    [data-testid="stMarkdownContainer"] p { text-align: center !important; }
    .stButton { display: flex; justify-content: center; }
    .stButton > button { width: 100%; border-radius: 10px; }
    .nutrition-box {
        background: #f7f9fc;
        border-radius: 12px;
        padding: 18px 14px;
        text-align: center;
        border: 1px solid #e2e8f0;
    }
    .nutrition-label { font-size: 13px; color: #6b7280; margin-bottom: 4px; }
    .nutrition-value { font-size: 24px; font-weight: 700; color: #1e293b; }
    .nutrition-unit { font-size: 12px; color: #94a3b8; }
    .log-item {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 6px;
        text-align: right;
    }
    .macro-bar-label {
        font-size: 13px;
        color: #374151;
        text-align: right;
        margin-bottom: 2px;
    }
    [data-testid="metric-container"] { text-align: center !important; }
</style>
""", unsafe_allow_html=True)

# --- Session state ---
for key, default in [
    ("user_email", None),
    ("daily_goal", None),
    ("carbs_goal", None),
    ("fat_goal", None),
    ("protein_goal", None),
    ("log", []),
    ("analysis_result", None),
    ("last_uploaded_name", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# --- Supabase via REST ---
def sb_headers():
    key = st.secrets["SUPABASE_KEY"].replace("\n", "").replace(" ", "").strip()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_url(table):
    return f"{st.secrets['SUPABASE_URL']}/rest/v1/{table}"


def load_settings(email: str):
    r = requests.get(sb_url("user_settings"), headers=sb_headers(),
                     params={"user_email": f"eq.{email}", "select": "*"})
    data = r.json()
    return data[0] if data else None


def save_settings(email, daily_goal, carbs_goal, fat_goal, protein_goal):
    payload = {
        "user_email": email,
        "daily_goal": daily_goal,
        "carbs_goal": carbs_goal,
        "fat_goal": fat_goal,
        "protein_goal": protein_goal,
    }
    h = {**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    requests.post(sb_url("user_settings"), headers=h, json=payload)


def load_today_log(email: str):
    today = date.today().isoformat()
    r = requests.get(sb_url("food_log"), headers=sb_headers(),
                     params={"user_email": f"eq.{email}", "log_date": f"eq.{today}",
                             "select": "*", "order": "id"})
    return [{
        "id": row["id"],
        "date": row["log_date"],
        "time": row["log_time"] or "",
        "name": row["food_name"],
        "calories": row["calories"],
        "carbs": row["carbs"],
        "fat": row["fat"],
        "protein": row["protein"],
    } for row in r.json()]


def load_all_log(email: str):
    r = requests.get(sb_url("food_log"), headers=sb_headers(),
                     params={"user_email": f"eq.{email}", "select": "*", "order": "id"})
    return r.json()


def add_food_entry(email: str, item: dict) -> int:
    payload = {
        "user_email": email,
        "log_date": item["date"],
        "log_time": item["time"],
        "food_name": item["name"],
        "calories": item["calories"],
        "carbs": item["carbs"],
        "fat": item["fat"],
        "protein": item["protein"],
    }
    r = requests.post(sb_url("food_log"), headers=sb_headers(), json=payload)
    return r.json()[0]["id"]


def delete_food_entry(entry_id: int):
    requests.delete(sb_url("food_log"), headers=sb_headers(),
                    params={"id": f"eq.{entry_id}"})


# --- Helpers ---
def get_media_type(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")


def analyze_food_image(image_bytes: bytes, media_type: str) -> dict:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    image_b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = """אתה מומחה תזונה. זהה את המזון בתמונה והחזר הערכת ערכים תזונתיים.

החזר JSON בלבד, בלי טקסט נוסף, בפורמט הבא:
{
  "name": "שם המזון בעברית",
  "serving_description": "תיאור המנה (למשל: 100 גרם / כוס אחת / ביצה אחת)",
  "calories": <מספר שלם>,
  "carbs": <גרם פחמימות, מספר שלם>,
  "fat": <גרם שומן, מספר שלם>,
  "protein": <גרם חלבון, מספר שלם>,
  "confidence": "high/medium/low"
}

אם לא ניתן לזהות מזון בתמונה, החזר: {"error": "לא זוהה מזון בתמונה"}"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt}
        ]}]
    )
    text = response.content[0].text.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"error": "שגיאה בניתוח התגובה"}


def get_meal_suggestions(remaining_calories: int) -> list:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    prompt = f"""אתה דיאטן מוסמך. נשארו למשתמש {remaining_calories} קלוריות להיום.
הצע 3 ארוחות מתאימות, כל אחת בטווח שבין 150 ל-{remaining_calories} קלוריות.

החזר JSON בלבד:
[
  {{"name": "שם הארוחה בעברית", "calories": <מספר>, "description": "תיאור קצר ומפתה בעברית"}},
  {{"name": "...", "calories": <מספר>, "description": "..."}},
  {{"name": "...", "calories": <מספר>, "description": "..."}}
]"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return []


def total_consumed():
    return sum(item["calories"] for item in st.session_state.log)


def total_macro(key):
    return sum(item[key] for item in st.session_state.log)


def macro_bar(label, consumed, goal, color):
    pct = min(consumed / goal, 1.0) * 100 if goal else 0
    bar_color = "#ef4444" if consumed > goal else color
    st.markdown(f"""
    <div class="macro-bar-label">{label}: {consumed}g / {goal}g</div>
    <div style="background:#e5e7eb;border-radius:6px;height:10px;overflow:hidden;margin-bottom:10px;">
        <div style="background:{bar_color};width:{pct:.1f}%;height:100%;border-radius:6px;"></div>
    </div>
    """, unsafe_allow_html=True)


def build_csv_all() -> bytes:
    rows = load_all_log(st.session_state.user_email)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["date", "time", "name", "calories", "carbs", "fat", "protein"])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "date": r["log_date"],
            "time": r["log_time"] or "",
            "name": r["food_name"],
            "calories": r["calories"],
            "carbs": r["carbs"],
            "fat": r["fat"],
            "protein": r["protein"],
        })
    return output.getvalue().encode("utf-8-sig")


# ===== SCREEN 0: LOGIN =====
if st.session_state.user_email is None:
    st.markdown("<br>", unsafe_allow_html=True)
    st.title("🍽️ מזהה קלוריות חכם")
    st.markdown("### התחברות")
    st.markdown("---")
    email_input = st.text_input("📧 אימייל")
    password_input = st.text_input("🔑 סיסמה", type="password")
    st.markdown("<br>", unsafe_allow_html=True)

    if st.button("✅ התחבר", type="primary"):
        try:
            users = json.loads(st.secrets["USERS"])
        except Exception:
            users = {}

        if users.get(email_input) == password_input and password_input:
            st.session_state.user_email = email_input
            settings = load_settings(email_input)
            if settings:
                st.session_state.daily_goal = settings["daily_goal"]
                st.session_state.carbs_goal = settings["carbs_goal"]
                st.session_state.fat_goal = settings["fat_goal"]
                st.session_state.protein_goal = settings["protein_goal"]
            st.session_state.log = load_today_log(email_input)
            st.rerun()
        else:
            st.error("אימייל או סיסמה שגויים")
    st.stop()


# ===== SCREEN 1: SET GOALS =====
elif st.session_state.daily_goal is None:
    st.markdown("<br>", unsafe_allow_html=True)
    st.title("🍽️ מזהה קלוריות חכם")
    st.markdown(f"### ברוך הבא!")
    st.markdown("#### הגדר יעדים יומיים")
    st.markdown("---")

    goal = st.number_input("🔥 קלוריות ליום", min_value=800, max_value=5000, value=2000, step=50)
    col1, col2, col3 = st.columns(3)
    with col1:
        carbs_goal = st.number_input("🍞 פחמימות (גרם)", min_value=20, max_value=600, value=250, step=10)
    with col2:
        fat_goal = st.number_input("🥑 שומן (גרם)", min_value=10, max_value=300, value=70, step=5)
    with col3:
        protein_goal = st.number_input("💪 חלבון (גרם)", min_value=10, max_value=300, value=100, step=5)

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("✅ התחל לעקוב", type="primary"):
        st.session_state.daily_goal = goal
        st.session_state.carbs_goal = carbs_goal
        st.session_state.fat_goal = fat_goal
        st.session_state.protein_goal = protein_goal
        save_settings(st.session_state.user_email, goal, carbs_goal, fat_goal, protein_goal)
        st.rerun()


# ===== SCREEN 2: MAIN APP =====
else:
    consumed = total_consumed()
    remaining = max(0, st.session_state.daily_goal - consumed)
    progress_pct = min(consumed / st.session_state.daily_goal, 1.0)
    over_limit = consumed > st.session_state.daily_goal

    # Header + logout
    col_title, col_logout = st.columns([4, 1])
    with col_title:
        st.title("🍽️ מזהה קלוריות חכם")
    with col_logout:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("יציאה"):
            for key in ["user_email", "daily_goal", "carbs_goal", "fat_goal",
                        "protein_goal", "log", "analysis_result", "last_uploaded_name"]:
                st.session_state[key] = [] if key == "log" else None
            st.rerun()

    # Daily summary
    col1, col2, col3 = st.columns(3)
    col1.metric("🎯 יעד יומי", f"{st.session_state.daily_goal:,} קל׳")
    col2.metric("🔥 נצרך", f"{consumed:,} קל׳")
    col3.metric(
        "✅ נשאר" if not over_limit else "⚠️ חריגה",
        f"{remaining:,} קל׳" if not over_limit else f"{consumed - st.session_state.daily_goal:,} קל׳",
        delta_color="off"
    )

    bar_color = "#ef4444" if over_limit else "#22c55e" if progress_pct < 0.85 else "#f59e0b"
    st.markdown(f"""
    <div style="background:#e5e7eb;border-radius:8px;height:14px;overflow:hidden;margin-bottom:4px;">
        <div style="background:{bar_color};width:{progress_pct*100:.1f}%;height:100%;border-radius:8px;"></div>
    </div>
    <div style="text-align:center;font-size:12px;color:#9ca3af;">{progress_pct*100:.0f}% מהיעד הקלורי היומי</div>
    """, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    macro_bar("🍞 פחמימות", total_macro("carbs"), st.session_state.carbs_goal, "#3b82f6")
    macro_bar("🥑 שומן", total_macro("fat"), st.session_state.fat_goal, "#f59e0b")
    macro_bar("💪 חלבון", total_macro("protein"), st.session_state.protein_goal, "#8b5cf6")

    st.markdown("---")

    # Image upload
    st.subheader("📸 העלה תמונת מזון")
    uploaded = st.file_uploader("בחר תמונה (JPG, PNG, WEBP)", type=["jpg", "jpeg", "png", "webp"])

    if uploaded:
        if uploaded.name != st.session_state.last_uploaded_name:
            st.session_state.analysis_result = None
            st.session_state.last_uploaded_name = uploaded.name
        st.image(uploaded, use_column_width=True)
        if st.button("🔍 זהה וחשב קלוריות", type="primary"):
            image_bytes = uploaded.read()
            media_type = get_media_type(uploaded.name)
            with st.spinner("מנתח את המזון..."):
                try:
                    st.session_state.analysis_result = analyze_food_image(image_bytes, media_type)
                except Exception as e:
                    st.session_state.analysis_result = {"error": f"שגיאה: {e}"}

    # Analysis result
    if st.session_state.analysis_result:
        result = st.session_state.analysis_result
        if "error" in result:
            st.error(result["error"])
        else:
            confidence_labels = {"high": ("✅", "ביטחון גבוה"), "medium": ("🟡", "ביטחון בינוני"), "low": ("⚠️", "ביטחון נמוך")}
            conf_icon, conf_text = confidence_labels.get(result.get("confidence", "medium"), ("🟡", ""))
            st.success(f"**{result['name']}** - {result.get('serving_description', '')}")
            if result.get("confidence") != "high":
                st.caption(f"{conf_icon} {conf_text} - ההערכה עשויה להשתנות בהתאם לגודל המנה בפועל")

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown(f"""<div class="nutrition-box">
                    <div class="nutrition-label">🔥 קלוריות</div>
                    <div class="nutrition-value">{result['calories']}</div>
                    <div class="nutrition-unit">kcal</div></div>""", unsafe_allow_html=True)
            with c2:
                st.markdown(f"""<div class="nutrition-box">
                    <div class="nutrition-label">🍞 פחמימות</div>
                    <div class="nutrition-value">{result['carbs']}</div>
                    <div class="nutrition-unit">גרם</div></div>""", unsafe_allow_html=True)
            with c3:
                st.markdown(f"""<div class="nutrition-box">
                    <div class="nutrition-label">🥑 שומן</div>
                    <div class="nutrition-value">{result['fat']}</div>
                    <div class="nutrition-unit">גרם</div></div>""", unsafe_allow_html=True)
            with c4:
                st.markdown(f"""<div class="nutrition-box">
                    <div class="nutrition-label">💪 חלבון</div>
                    <div class="nutrition-value">{result['protein']}</div>
                    <div class="nutrition-unit">גרם</div></div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("➕ הוסף ליומן האכילה", type="secondary"):
                now = datetime.now()
                item = {
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%H:%M"),
                    "name": result["name"],
                    "calories": result["calories"],
                    "carbs": result["carbs"],
                    "fat": result["fat"],
                    "protein": result["protein"],
                }
                entry_id = add_food_entry(st.session_state.user_email, item)
                item["id"] = entry_id
                st.session_state.log.append(item)
                st.session_state.analysis_result = None
                st.session_state.last_uploaded_name = None
                st.success("נוסף ליומן!")
                st.rerun()

    st.markdown("---")

    # Food log
    if st.session_state.log:
        st.subheader("📋 יומן אכילה - היום")
        col_dl, _ = st.columns([2, 3])
        with col_dl:
            st.download_button(
                label="⬇️ הורד כל ההיסטוריה (CSV)",
                data=build_csv_all(),
                file_name=f"יומן_אכילה_{datetime.now().strftime('%Y-%m-%d')}.csv",
                mime="text/csv",
            )
        for i, item in enumerate(st.session_state.log):
            col_a, col_b = st.columns([5, 1])
            with col_a:
                st.markdown(
                    f"""<div class="log-item">
                        <span style="font-size:11px;color:#9ca3af;">{item.get('date','')} {item.get('time','')}</span><br>
                        <b>{item['name']}</b> &nbsp;|&nbsp;
                        {item['calories']} קל׳ &nbsp;|&nbsp;
                        פחמ׳ {item['carbs']}g &nbsp;|&nbsp;
                        שומן {item['fat']}g &nbsp;|&nbsp;
                        חלב׳ {item['protein']}g
                    </div>""",
                    unsafe_allow_html=True
                )
            with col_b:
                if st.button("🗑️", key=f"del_{i}", help="הסר מהיומן"):
                    if "id" in item:
                        delete_food_entry(item["id"])
                    st.session_state.log.pop(i)
                    st.rerun()
        st.markdown("---")

    # Meal suggestions
    remaining_for_suggestions = st.session_state.daily_goal - consumed
    if remaining_for_suggestions >= 150:
        st.subheader("💡 המלצות ארוחה לפי יתרת הקלוריות")
        st.caption(f"נשארו לך {remaining_for_suggestions:,} קלוריות להיום")
        if st.button("🎯 הצע ארוחות מתאימות"):
            with st.spinner("מחפש המלצות בשבילך..."):
                try:
                    suggestions = get_meal_suggestions(remaining_for_suggestions)
                    for s in suggestions:
                        with st.expander(f"🍴 {s['name']} - {s['calories']} קל׳"):
                            st.write(s.get("description", ""))
                except Exception as e:
                    st.error(f"שגיאה בקבלת המלצות: {e}")
    elif consumed > 0:
        st.info("✅ הגעת ליעד הקלורי היומי שלך!")

    st.markdown("---")
    if st.button("🔄 איפוס יעדים - יום חדש"):
        for key in ["daily_goal", "carbs_goal", "fat_goal", "protein_goal"]:
            st.session_state[key] = None
        st.session_state.log = []
        st.rerun()
