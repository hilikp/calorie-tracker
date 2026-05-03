import streamlit as st
import anthropic
import base64
import json
import re
import csv
import io
from datetime import datetime, date
import gspread
from google.oauth2.service_account import Credentials

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
    p, .stCaption, [data-testid="stCaptionContainer"] p,
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
    ("username", None),
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


# --- Google Sheets ---
@st.cache_resource
def get_gsheet():
    creds_info = json.loads(st.secrets["GSHEET_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(st.secrets["GSHEET_ID"])


def load_settings(username: str):
    try:
        ws = get_gsheet().worksheet("user_settings")
        records = ws.get_all_records()
        for r in records:
            if str(r.get("username", "")).strip() == username:
                return r
    except Exception:
        pass
    return None


def save_settings(username, daily_goal, carbs_goal, fat_goal, protein_goal):
    try:
        ws = get_gsheet().worksheet("user_settings")
        records = ws.get_all_records()
        for i, r in enumerate(records):
            if str(r.get("username", "")).strip() == username:
                row_num = i + 2
                ws.update(f"A{row_num}:E{row_num}",
                          [[username, daily_goal, carbs_goal, fat_goal, protein_goal]])
                return
        ws.append_row([username, daily_goal, carbs_goal, fat_goal, protein_goal])
    except Exception as e:
        st.warning(f"שגיאה בשמירת הגדרות: {e}")


def load_today_log(username: str):
    try:
        ws = get_gsheet().worksheet("food_log")
        records = ws.get_all_records()
        today = date.today().isoformat()
        result = []
        for r in records:
            if str(r.get("username", "")).strip() == username and r.get("date") == today:
                result.append({
                    "id": r.get("row_id", ""),
                    "date": r.get("date", ""),
                    "time": r.get("time", ""),
                    "name": r.get("food_name", ""),
                    "calories": int(r.get("calories") or 0),
                    "carbs": int(r.get("carbs") or 0),
                    "fat": int(r.get("fat") or 0),
                    "protein": int(r.get("protein") or 0),
                })
        return result
    except Exception:
        return []


def add_food_entry(username: str, item: dict) -> str:
    entry_id = f"{username}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    ws = get_gsheet().worksheet("food_log")
    ws.append_row([
        entry_id, username, item["date"], item["time"],
        item["name"], item["calories"], item["carbs"],
        item["fat"], item["protein"]
    ])
    return entry_id


def delete_food_entry(entry_id: str):
    try:
        ws = get_gsheet().worksheet("food_log")
        cell = ws.find(entry_id)
        if cell:
            ws.delete_rows(cell.row)
    except Exception:
        pass


def load_all_log(username: str):
    try:
        ws = get_gsheet().worksheet("food_log")
        records = ws.get_all_records()
        return [r for r in records if str(r.get("username", "")).strip() == username]
    except Exception:
        return []


def build_csv_all() -> bytes:
    rows = load_all_log(st.session_state.username)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["date", "time", "food_name", "calories", "carbs", "fat", "protein"])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "date": r.get("date", ""),
            "time": r.get("time", ""),
            "food_name": r.get("food_name", ""),
            "calories": r.get("calories", ""),
            "carbs": r.get("carbs", ""),
            "fat": r.get("fat", ""),
            "protein": r.get("protein", ""),
        })
    return output.getvalue().encode("utf-8-sig")


# --- AI functions ---
def get_media_type(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")


def analyze_food_image(image_bytes: bytes, media_type: str, description: str = "") -> dict:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    image_b64 = base64.standard_b64encode(image_bytes).decode()
    description_note = f'\n\nהמשתמש הוסיף: "{description.strip()}"' if description.strip() else ""
    prompt = f"""אתה מומחה תזונה. זהה את המזון בתמונה והחזר הערכת ערכים תזונתיים.{description_note}

החזר JSON בלבד:
{{
  "name": "שם המזון בעברית",
  "serving_description": "תיאור המנה",
  "calories": <מספר שלם>,
  "carbs": <גרם פחמימות, מספר שלם>,
  "fat": <גרם שומן, מספר שלם>,
  "protein": <גרם חלבון, מספר שלם>,
  "confidence": "high/medium/low"
}}

אם לא ניתן לזהות מזון בתמונה, החזר: {{"error": "לא זוהה מזון בתמונה"}}"""
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


def calculate_nutrition_from_text(food_name: str, amount: str) -> dict:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    prompt = f"""אתה מומחה תזונה. חשב ערכים תזונתיים עבור: {food_name}, כמות: {amount}.

החזר JSON בלבד:
{{
  "name": "שם המזון בעברית",
  "serving_description": "תיאור המנה",
  "calories": <מספר שלם>,
  "carbs": <גרם פחמימות, מספר שלם>,
  "fat": <גרם שומן, מספר שלם>,
  "protein": <גרם חלבון, מספר שלם>,
  "confidence": "high/medium/low"
}}"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"error": "שגיאה בחישוב"}


def get_meal_suggestions(remaining_calories: int) -> list:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    prompt = f"""אתה דיאטן מוסמך. נשארו למשתמש {remaining_calories} קלוריות להיום.
הצע 3 ארוחות מתאימות בין 150 ל-{remaining_calories} קלוריות.

החזר JSON בלבד:
[
  {{"name": "שם הארוחה בעברית", "calories": <מספר>, "description": "תיאור קצר בעברית"}},
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


def show_confirm_form(result):
    """Shared confirmation/edit form used by both image and manual entry."""
    confidence_labels = {"high": "✅ ביטחון גבוה", "medium": "🟡 ביטחון בינוני", "low": "⚠️ ביטחון נמוך"}
    conf = confidence_labels.get(result.get("confidence", "medium"), "🟡")
    st.info(f"🤖 זוהה: **{result['name']}** - {result.get('serving_description','')} | {conf}")

    with st.form("confirm_form"):
        st.markdown("#### ✏️ אשר או תקן לפני הוספה ליומן")
        name_input = st.text_input("שם המזון", value=result["name"])
        c1, c2, c3, c4 = st.columns(4)
        cal_input = c1.number_input("🔥 קלוריות", value=int(result["calories"]), min_value=0)
        carb_input = c2.number_input("🍞 פחמימות g", value=int(result["carbs"]), min_value=0)
        fat_input = c3.number_input("🥑 שומן g", value=int(result["fat"]), min_value=0)
        prot_input = c4.number_input("💪 חלבון g", value=int(result["protein"]), min_value=0)

        col_ok, col_cancel = st.columns(2)
        confirmed = col_ok.form_submit_button("✅ הוסף ליומן", type="primary", use_container_width=True)
        cancelled = col_cancel.form_submit_button("❌ בטל", use_container_width=True)

    if confirmed:
        now = datetime.now()
        item = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "name": name_input,
            "calories": cal_input,
            "carbs": carb_input,
            "fat": fat_input,
            "protein": prot_input,
        }
        with st.spinner("שומר..."):
            try:
                entry_id = add_food_entry(st.session_state.username, item)
                item["id"] = entry_id
                st.session_state.log.append(item)
                st.session_state.analysis_result = None
                st.session_state.last_uploaded_name = None
                st.success("✅ נוסף ליומן!")
                st.rerun()
            except Exception as e:
                st.error(f"שגיאה בשמירה: {e}")

    if cancelled:
        st.session_state.analysis_result = None
        st.session_state.last_uploaded_name = None
        st.rerun()


# ===== SCREEN 0: LOGIN =====
if st.session_state.username is None:
    st.markdown("<br>", unsafe_allow_html=True)
    st.title("🍽️ מזהה קלוריות חכם")
    st.markdown("### התחברות")
    st.markdown("---")
    username_input = st.text_input("👤 שם משתמש", placeholder="הכנס שם משתמש", key="login_user")
    password_input = st.text_input("🔑 סיסמה", type="password", placeholder="הכנס סיסמה", key="login_pass")
    st.markdown("<br>", unsafe_allow_html=True)

    if st.button("✅ התחבר", type="primary"):
        try:
            valid_user = st.secrets["USER1_NAME"].strip()
            valid_pass = st.secrets["USER1_PASSWORD"].strip()
        except Exception as ex:
            st.error(f"Secrets error: {ex}")
            st.stop()

        if username_input.strip() == valid_user and password_input.strip() == valid_pass:
            st.session_state.username = username_input.strip()
            with st.spinner("טוען נתונים..."):
                try:
                    settings = load_settings(username_input.strip())
                    if settings:
                        st.session_state.daily_goal = int(settings.get("daily_goal") or 2000)
                        st.session_state.carbs_goal = int(settings.get("carbs_goal") or 250)
                        st.session_state.fat_goal = int(settings.get("fat_goal") or 70)
                        st.session_state.protein_goal = int(settings.get("protein_goal") or 100)
                    st.session_state.log = load_today_log(username_input.strip())
                except Exception as e:
                    st.warning(f"לא ניתן לטעון נתונים: {e}")
            st.rerun()
        else:
            st.error("שם משתמש או סיסמה שגויים")
    st.stop()


# ===== SCREEN 1: SET GOALS =====
elif st.session_state.daily_goal is None:
    st.markdown("<br>", unsafe_allow_html=True)
    st.title("🍽️ מזהה קלוריות חכם")
    st.markdown(f"### שלום {st.session_state.username}!")
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
        with st.spinner("שומר..."):
            save_settings(st.session_state.username, goal, carbs_goal, fat_goal, protein_goal)
        st.rerun()


# ===== SCREEN 2: MAIN APP =====
else:
    consumed = total_consumed()
    remaining = max(0, st.session_state.daily_goal - consumed)
    progress_pct = min(consumed / st.session_state.daily_goal, 1.0)
    over_limit = consumed > st.session_state.daily_goal

    col_title, col_logout = st.columns([4, 1])
    with col_title:
        st.title("🍽️ מזהה קלוריות חכם")
    with col_logout:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("יציאה"):
            for key in ["username", "daily_goal", "carbs_goal", "fat_goal",
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

    # ---- Input tabs ----
    tab1, tab2 = st.tabs(["📸 העלה תמונה", "✏️ הזן ידנית"])

    with tab1:
        uploaded = st.file_uploader("בחר תמונה (JPG, PNG, WEBP)", type=["jpg", "jpeg", "png", "webp"])
        if uploaded:
            if uploaded.name != st.session_state.last_uploaded_name:
                st.session_state.analysis_result = None
                st.session_state.last_uploaded_name = uploaded.name
            st.image(uploaded, use_column_width=True)
            food_description = st.text_area(
                "📝 תיאור נוסף (אופציונלי)",
                placeholder="למשל: שניצל עוף מטוגן, מנה של כ-300 גרם...",
                height=80,
            )
            if st.button("🔍 זהה וחשב קלוריות", type="primary"):
                image_bytes = uploaded.read()
                media_type = get_media_type(uploaded.name)
                with st.spinner("מנתח את המזון..."):
                    try:
                        st.session_state.analysis_result = analyze_food_image(image_bytes, media_type, food_description)
                    except Exception as e:
                        st.session_state.analysis_result = {"error": f"שגיאה: {e}"}

    with tab2:
        st.markdown("#### הזן פרטי מזון ידנית")
        food_name_manual = st.text_input("🍽️ שם המזון", placeholder="למשל: אורז לבן מבושל")
        food_amount_manual = st.text_input("⚖️ כמות / משקל", placeholder="למשל: 200 גרם / כוס אחת / 3 יחידות")

        if st.button("🔢 חשב ערכים תזונתיים", type="primary", disabled=not food_name_manual):
            with st.spinner("מחשב ערכים תזונתיים..."):
                try:
                    result = calculate_nutrition_from_text(food_name_manual, food_amount_manual or "מנה אחת")
                    st.session_state.analysis_result = result
                    st.session_state.last_uploaded_name = None
                except Exception as e:
                    st.session_state.analysis_result = {"error": f"שגיאה: {e}"}

    # ---- Confirmation form (shared) ----
    if st.session_state.analysis_result:
        result = st.session_state.analysis_result
        st.markdown("---")
        if "error" in result:
            st.error(result["error"])
            if st.button("נסה שוב"):
                st.session_state.analysis_result = None
                st.rerun()
        else:
            show_confirm_form(result)

    st.markdown("---")

    # ---- Food log ----
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
                    with st.spinner("מוחק..."):
                        delete_food_entry(item.get("id", ""))
                    st.session_state.log.pop(i)
                    st.rerun()
        st.markdown("---")

    # ---- Meal suggestions ----
    remaining_for_suggestions = st.session_state.daily_goal - consumed
    if remaining_for_suggestions >= 150:
        st.subheader("💡 המלצות ארוחה")
        st.caption(f"נשארו לך {remaining_for_suggestions:,} קלוריות להיום")
        if st.button("🎯 הצע ארוחות מתאימות"):
            with st.spinner("מחפש המלצות..."):
                try:
                    suggestions = get_meal_suggestions(remaining_for_suggestions)
                    for s in suggestions:
                        with st.expander(f"🍴 {s['name']} - {s['calories']} קל׳"):
                            st.write(s.get("description", ""))
                except Exception as e:
                    st.error(f"שגיאה: {e}")
    elif consumed > 0:
        st.info("✅ הגעת ליעד הקלורי היומי שלך!")

    st.markdown("---")
    if st.button("🔄 איפוס יעדים - יום חדש"):
        for key in ["daily_goal", "carbs_goal", "fat_goal", "protein_goal"]:
            st.session_state[key] = None
        st.session_state.log = []
        st.rerun()
