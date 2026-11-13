#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
هوش مصنوعی آفلاین با کتابخانه دانش
اجرا:  python offline_ai.py     سپس مرورگر:  http://127.0.0.1:8080
فقط کتابخانه استاندارد پایتون (3.8+) لازم است.
"""
import bz2, json, os, re, sqlite3, sys, threading, time, urllib.request, urllib.error, tempfile
import importlib, importlib.util, socket, subprocess
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
PACKS_DIR = os.path.join(DATA, "packs")
DB_PATH = os.path.join(DATA, "library.db")
os.makedirs(PACKS_DIR, exist_ok=True)

PORT = int(os.environ.get("PORT", 8080))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")

CATALOG = [
    {"id": "fawiki", "name": "ویکی‌پدیای فارسی",
     "desc": "همه مقاله‌های ویکی‌پدیای فارسی (فایل فشرده حدود ۱ تا ۲ گیگابایت)",
     "url": "https://dumps.wikimedia.org/fawiki/latest/fawiki-latest-pages-articles.xml.bz2",
     "kind": "wikidump"},
    {"id": "enwiki_simple", "name": "Simple English Wikipedia",
     "desc": "ویکی‌پدیای ساده انگلیسی (کوچک‌تر، برای تست سریع)",
     "url": "https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2",
     "kind": "wikidump"},
]

# ابزارهای اختیاری که کاربر از داخل خود برنامه (کتابخانه) نصب می‌کند
MODELS_DIR = os.path.join(DATA, "models")
TOOLS = [
    {"id": "tool_stt", "name": "ابزار ویس به متن (Whisper)",
     "desc": "برای دکمه 🎙 ویس. نصب پکیج faster-whisper و دانلود مدل (حدود ۵۰۰ مگابایت). فقط یک بار لازم است.",
     "pip": ["faster-whisper"], "repo": "Systran/faster-whisper-small",
     "dir": os.path.join(MODELS_DIR, "whisper-small")},
    {"id": "tool_tts", "name": "ابزار صدای پاسخ (ساخت ویس)",
     "desc": "برای دکمه «دانلود ویس پاسخ». نصب پکیج pyttsx3 (از صداهای خود سیستم استفاده می‌کند).",
     "pip": ["pyttsx3"], "repo": None, "dir": None},
]
TOOL_BY_ID = {t["id"]: t for t in TOOLS}

_db_lock = threading.Lock()

def db():
    c = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def delete_chunks(c, pid):
    ids = [r[0] for r in c.execute("SELECT rowid FROM chunks WHERE pack=?", (pid,))]
    c.execute("DELETE FROM chunks WHERE pack=?", (pid,))
    c.executemany("DELETE FROM raw WHERE rowid=?", [(i,) for i in ids])

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS packs(
        id TEXT PRIMARY KEY, name TEXT, desc TEXT, url TEXT, kind TEXT,
        status TEXT DEFAULT 'none', enabled INTEGER DEFAULT 0,
        progress REAL DEFAULT 0, msg TEXT DEFAULT '', custom INTEGER DEFAULT 0)""")
    c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        title, body, pack UNINDEXED, tokenize='unicode61 remove_diacritics 2')""")
    c.execute("""CREATE TABLE IF NOT EXISTS profile(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
        age TEXT DEFAULT '', gender TEXT DEFAULT '', favorite_color TEXT DEFAULT '',
        bio TEXT DEFAULT '', updated_at REAL DEFAULT 0)""")
    c.execute("INSERT OR IGNORE INTO profile(id) VALUES(1)")
    c.execute("CREATE TABLE IF NOT EXISTS raw(rowid INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    for t in TOOLS:
        c.execute("INSERT OR IGNORE INTO packs(id,name,desc,url,kind) VALUES(?,?,?,?,?)",
                  (t["id"], t["name"], t["desc"], "", "tool"))
        c.execute("UPDATE packs SET name=?, desc=? WHERE id=?", (t["name"], t["desc"], t["id"]))
    # فایل تست قدیمی حذف می‌شود
    delete_chunks(c, "starter_test")
    c.execute("DELETE FROM packs WHERE id='starter_test'")
    try: os.remove(os.path.join(PACKS_DIR, "starter_test.txt"))
    except OSError: pass
    for p in CATALOG:
        c.execute("INSERT OR IGNORE INTO packs(id,name,desc,url,kind) VALUES(?,?,?,?,?)",
                  (p["id"], p["name"], p["desc"], p["url"], p["kind"]))
    c.execute("UPDATE packs SET status='none', msg='قطع شد؛ دوباره دانلود را بزن' "
              "WHERE status IN ('downloading','indexing')")
    c.commit(); c.close()

def set_pack(pid, **kw):
    with _db_lock:
        c = db()
        sets = ",".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE packs SET {sets} WHERE id=?", (*kw.values(), pid))
        c.commit(); c.close()

def get_profile():
    c = db()
    row = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    c.close()
    return dict(row) if row else {}

def save_profile(data):
    with _db_lock:
        c = db()
        c.execute("""UPDATE profile SET
            first_name=?, last_name=?, age=?, gender=?, favorite_color=?, bio=?, updated_at=?
            WHERE id=1""",
            (data.get("first_name",""), data.get("last_name",""), data.get("age",""),
             data.get("gender",""), data.get("favorite_color",""), data.get("bio",""),
             time.time()))
        c.commit(); c.close()

_FA = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و",
                     "\u200c": " ", "\u200f": "", "\u200e": ""})
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def norm(s):
    return s.translate(_FA).translate(_DIG)

STOP = set(norm("از در به که را با این آن و یا هم است بود شد برای چه چی کی کجا چگونه چرا آیا "
                "من تو ما شما او می‌شود های ها ای یک بر تا اما اگر نیز همه چند کدام چیست کیست "
                "چیه کیه درباره بگو بگین توضیح بده بدی رو کن کنید میشه کنم چطوره چطور میخوام "
                "the a an of in on to is are was were what who how why when where and or").split())

def clean_wiki(t):
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<ref[^>/]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    for _ in range(6):
        n = re.sub(r"\{\{[^{}]*\}\}", "", t)
        if n == t: break
        t = n
    t = re.sub(r"\{\|.*?\|\}", "", t, flags=re.S)
    t = re.sub(r"\[\[(?:پرونده|تصویر|File|Image|رده|Category)[^\]]*(?:\[\[[^\]]*\]\][^\]]*)*\]\]", "", t, flags=re.I)
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://[^\s\]]*\s*([^\]]*)\]", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"^=+\s*(.*?)\s*=+\s*$", r"\1", t, flags=re.M)
    t = re.sub(r"^[*#:;]+\s*", "", t, flags=re.M)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def chunk_text(text, size=900):
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para: continue
        if len(cur) + len(para) > size and cur:
            out.append(cur); cur = ""
        while len(para) > size * 1.5:
            cut = para.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            out.append((cur + " " + para[:cut]).strip()); cur = ""; para = para[cut:].strip()
        cur = (cur + "\n" + para).strip()
    if cur: out.append(cur)
    return out

def add_chunks(c, pid, title, text, size=900):
    for ch in chunk_text(text, size):
        if len(ch) < 40: continue
        c.execute("INSERT INTO chunks(title,body,pack) VALUES(?,?,?)", (norm(title), norm(ch), pid))
        c.execute("INSERT OR REPLACE INTO raw(rowid,title,body) VALUES(last_insert_rowid(),?,?)", (title, ch))

def download_pack(pid):
    c = db(); row = c.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone(); c.close()
    if not row: return
    if row["kind"] == "tool":
        install_tool(pid); return
    dest = os.path.join(PACKS_DIR, pid + ".download")
    try:
        set_pack(pid, status="downloading", progress=0, msg="در حال دانلود…")
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        req = urllib.request.Request(row["url"], headers={"User-Agent": "OfflineAI/1.0"})
        if have: req.add_header("Range", f"bytes={have}-")
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0))
            if r.status == 200: have = 0
            total += have
            with open(dest, "ab" if have else "wb") as f:
                got, last = have, 0
                while True:
                    b = r.read(1 << 20)
                    if not b: break
                    f.write(b); got += len(b)
                    if time.time() - last > 1:
                        last = time.time()
                        pr = got / total if total else 0
                        set_pack(pid, progress=pr * 0.5,
                                 msg=f"دانلود {got/1e6:.0f} از {total/1e6:.0f} مگابایت")
        index_pack(pid, dest)
    except Exception as e:
        set_pack(pid, status="none", msg=f"خطا: {e}")

def index_pack(pid, path):
    c = db(); row = c.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone(); c.close()
    kind = row["kind"]
    set_pack(pid, status="indexing", progress=0.5, msg="در حال ساخت فهرست جستجو…")
    c = db()
    delete_chunks(c, pid)
    n = 0
    try:
        if kind == "wikidump":
            size = os.path.getsize(path)
            raw = open(path, "rb")
            stream = bz2.BZ2File(raw)
            title, ns, redirect, text = None, "0", False, None
            for ev, el in ET.iterparse(stream, events=("end",)):
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "title": title = el.text
                elif tag == "ns": ns = el.text
                elif tag == "redirect": redirect = True
                elif tag == "text": text = el.text
                elif tag == "page":
                    if ns == "0" and not redirect and text and title:
                        add_chunks(c, pid, title, clean_wiki(text))
                        n += 1
                        if n % 500 == 0:
                            c.commit()
                            set_pack(pid, progress=0.5 + 0.5 * min(raw.tell() / size, 1),
                                     msg=f"ایندکس {n} مقاله…")
                    title, ns, redirect, text = None, "0", False, None
                    el.clear()
        elif kind == "text":
            title = os.path.basename(path)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                add_chunks(c, pid, title, f.read()); n = 1
        c.commit(); c.close()
        set_pack(pid, status="ready", progress=1, msg=f"آماده ({n} سند)")
        if kind == "wikidump":
            try: os.remove(path)
            except OSError: pass
    except Exception as e:
        c.commit(); c.close()
        set_pack(pid, status="none", msg=f"خطا در ایندکس: {e}")

def tool_installed(pid):
    t = TOOL_BY_ID.get(pid)
    if not t: return False
    importlib.invalidate_caches()
    for pkg in t["pip"]:
        if importlib.util.find_spec(pkg.replace("-", "_")) is None:
            return False
    if t["dir"] and not os.path.exists(os.path.join(t["dir"], "model.bin")):
        return False
    return True

def _pip(pkgs):
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    out = ""
    for extra in ([], ["--break-system-packages"], ["--user"]):
        p = subprocess.run(base + extra + pkgs, capture_output=True, text=True)
        if p.returncode == 0:
            return
        out = (p.stderr or p.stdout or "").strip()
        if "externally-managed" not in out and extra == []:
            break
    raise RuntimeError(out.splitlines()[-1] if out else "pip failed")

def install_tool(pid):
    t = TOOL_BY_ID[pid]
    try:
        set_pack(pid, status="downloading", progress=0.1, msg="در حال نصب پکیج‌ها (نیاز به اینترنت)…")
        _pip(t["pip"])
        importlib.invalidate_caches()
        if t["repo"]:
            set_pack(pid, progress=0.4, msg="در حال دانلود مدل؛ ممکن است چند دقیقه طول بکشد…")
            from huggingface_hub import snapshot_download
            os.makedirs(t["dir"], exist_ok=True)
            snapshot_download(repo_id=t["repo"], local_dir=t["dir"])
        if tool_installed(pid):
            set_pack(pid, status="ready", progress=1, msg="نصب شد ✓")
        else:
            set_pack(pid, status="none", progress=0, msg="نصب کامل نشد؛ دوباره تلاش کن")
    except Exception as e:
        set_pack(pid, status="none", progress=0, msg=f"خطا در نصب: {e}")

def list_packs():
    c = db()
    rows = [dict(r) for r in c.execute("SELECT * FROM packs ORDER BY custom, (kind='tool'), rowid")]
    c.close()
    for r in rows:
        if r["kind"] == "tool" and r["status"] != "downloading":
            ok = tool_installed(r["id"])
            r["status"] = "ready" if ok else "none"
            r["msg"] = "نصب شده ✓" if ok else (r["msg"] if r["msg"].startswith("خطا") else "")
    return rows

def search(q, k=5):
    c = db()
    ids = [r["id"] for r in c.execute("SELECT id FROM packs WHERE enabled=1 AND status='ready'")]
    if not ids: c.close(); return None
    toks = [t for t in re.findall(r"\w+", norm(q).lower()) if len(t) > 1 and t not in STOP]
    if not toks: c.close(); return []
    match = " OR ".join('"%s"' % t.replace('"', "") for t in toks[:12])
    ph = ",".join("?" * len(ids))
    rows = c.execute(
        f"SELECT rowid, title, pack, bm25(chunks,6.0,1.0) AS s FROM chunks "
        f"WHERE chunks MATCH ? AND pack IN ({ph}) ORDER BY s LIMIT ?",
        (match, *ids, k)).fetchall()
    out = []
    for r in rows:
        raw = c.execute("SELECT title, body FROM raw WHERE rowid=?", (r["rowid"],)).fetchone()
        out.append({"title": raw["title"], "text": raw["body"], "pack": r["pack"]})
    c.close()
    return out

def llm_model():
    if OLLAMA_MODEL: return OLLAMA_MODEL
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            m = json.load(r).get("models", [])
            return m[0]["name"] if m else None
    except Exception:
        return None

def llm_stream(model, messages):
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if not line.strip(): continue
            d = json.loads(line)
            if d.get("message", {}).get("content"): yield d["message"]["content"]
            if d.get("done"): break

_voice_lock = threading.Lock()
_whisper_instance = None
_whisper_path = None

def _get_whisper(model_path):
    global _whisper_instance, _whisper_path
    if _whisper_instance is None or _whisper_path != model_path:
        from faster_whisper import WhisperModel
        _whisper_instance = WhisperModel(model_path, device="cpu", compute_type="int8")
        _whisper_path = model_path
    return _whisper_instance

def build_system_prompt(profile):
    base = ("تو یک دستیار آفلاین هستی. فقط بر اساس «منابع» زیر به زبان کاربر جواب بده. "
            "اگر جواب در منابع نیست، صادقانه بگو پیدا نکردی. "
            "فقط پاسخ نهایی را بنویس. سؤال کاربر را تکرار نکن. جواب را کوتاه و دقیق بنویس.")
    parts = []
    if profile.get("first_name") or profile.get("last_name"):
        name = (profile.get("first_name","") + " " + profile.get("last_name","")).strip()
        parts.append(f"نام کاربر: {name}")
    if profile.get("age"):
        parts.append(f"سن: {profile['age']}")
    if profile.get("gender"):
        parts.append(f"جنسیت: {profile['gender']}")
    if profile.get("favorite_color"):
        parts.append(f"رنگ مورد علاقه: {profile['favorite_color']}")
    if profile.get("bio"):
        parts.append(f"درباره کاربر و علایقش:\n{profile['bio']}")
    if parts:
        base += "\n\nاطلاعات شخصی کاربر (برای شخصی‌سازی پاسخ استفاده کن):\n" + "\n".join(parts)
    return base

ANIME_PACK_ID = "anime10"
ANIME_PACK_NAME = "معرفی ۱۰ انیمه"
ANIME_DOCS = [
 ("فهرست انیمه‌ها - لیست ۱۰ انیمه این بسته",
  "این بسته درباره‌ی ۱۰ انیمه است: ناروتو (Naruto)، وان پیس (One Piece)، حمله به تایتان (Attack on Titan)، "
  "دث نوت (Death Note)، فول متال آلکمیست برادرهود (Fullmetal Alchemist: Brotherhood)، شیطان‌کش (Demon Slayer)، "
  "آکادمی قهرمان من (My Hero Academia)، دراگون بال زد (Dragon Ball Z)، هانتر × هانتر (Hunter x Hunter) "
  "و جوجوتسو کایسن (Jujutsu Kaisen). درباره‌ی هرکدام داستان، شخصیت‌ها، سازنده و استودیو می‌توانی بپرسی."),
 ("ناروتو Naruto",
  "ناروتو (Naruto) یک انیمه‌ی اکشن و ماجراجویی از ژانر شونن است که از روی مانگای ماساشی کیشیموتو ساخته شد. "
  "استودیو پیرو (Pierrot) آن را از سال ۲۰۰۲ پخش کرد؛ ناروتو حدود ۲۲۰ قسمت دارد و ادامه‌اش ناروتو شیپودن (۲۰۰۷ تا ۲۰۱۷) حدود ۵۰۰ قسمت است.\n\n"
  "داستان: ناروتو اوزوماکی نینجای شیطنت‌کار دهکده‌ی کونوهاست. روباه نه‌دم درون بدن او مهر شده و مردم دهکده او را تنها گذاشته‌اند. "
  "او آرزو دارد هوکاگه (رهبر و قوی‌ترین نینجای دهکده) شود تا مردم او را به رسمیت بشناسند.\n\n"
  "شخصیت‌های اصلی: ناروتو اوزوماکی، ساسکه اوچیها (رقیب و دوست او)، ساکورا هارونو و معلمشان کاکاشی هاتاکه."),
 ("وان پیس One Piece",
  "وان پیس (One Piece) مانگایی از ایچیرو اودا است که از سال ۱۹۹۷ منتشر می‌شود و انیمه‌ی آن را استودیو توئی (Toei Animation) از سال ۱۹۹۹ می‌سازد. "
  "این انیمه صدها قسمت دارد و یکی از طولانی‌ترین و پرطرفدارترین انیمه‌هاست.\n\n"
  "داستان: مانکی دی لوفی با خوردن میوه‌ی شیطان «گومو گومو» بدنی لاستیکی پیدا کرده است. او با خدمه‌اش یعنی دزدان دریایی کلاه‌حصیری "
  "(زورو، نامی، اوسوپ، سانجی، چاپر، رابین، فرنکی، بروک و جینبه) در دریاها سفر می‌کند تا گنج افسانه‌ای «وان پیس» را پیدا کند و پادشاه دزدان دریایی شود."),
 ("حمله به تایتان Attack on Titan - شینگکی نو کیوجین",
  "حمله به تایتان (Attack on Titan یا Shingeki no Kyojin) از روی مانگای هاجیمه ایسایاما ساخته شده و از سال ۲۰۱۳ پخش شد. "
  "سه فصل اول را استودیو ویت (Wit Studio) و فصل پایانی را استودیو مپا (MAPPA) ساخت و در سال ۲۰۲۳ تمام شد.\n\n"
  "داستان: انسان‌ها پشت دیوارهای بزرگ زندگی می‌کنند تا از تایتان‌ها، غول‌های آدم‌خوار، در امان باشند. وقتی تایتانی دیوار را می‌شکند، "
  "ارن ییگر تصمیم می‌گیرد همه‌ی تایتان‌ها را نابود کند. او همراه میکاسا آکرمن و آرمین آرلرت به سپاه شناسایی می‌پیوندد."),
 ("دث نوت Death Note - دفترچه مرگ",
  "دث نوت (Death Note) از روی مانگای تسوگومی اوبا و تاکشی اوباتا ساخته شد. استودیو مدهاوس (Madhouse) آن را در سال‌های ۲۰۰۶ و ۲۰۰۷ "
  "در ۳۷ قسمت پخش کرد. ژانر آن معمایی، روانشناختی و هیجانی است.\n\n"
  "داستان: لایت یاگامی، دانش‌آموزی نابغه، دفترچه‌ای از شینیگامی (خدای مرگ) به نام ریوک پیدا می‌کند. هر کس نامش در دفترچه نوشته شود می‌میرد. "
  "لایت می‌خواهد با آن دنیا را از آدم‌های بد پاک کند و «کیرا» نامیده می‌شود. کارآگاه نابغه‌ای به نام ال (L) برای دستگیری او وارد می‌شود."),
 ("فول متال آلکمیست برادرهود Fullmetal Alchemist Brotherhood",
  "فول متال آلکمیست برادرهود (Fullmetal Alchemist: Brotherhood) از روی مانگای هیرومو آراکاوا ساخته شد. استودیو بونز (Bones) آن را "
  "در سال‌های ۲۰۰۹ و ۲۰۱۰ در ۶۴ قسمت پخش کرد و بسیاری آن را از بهترین انیمه‌های تاریخ می‌دانند.\n\n"
  "داستان: ادوارد و آلفونس الریک دو برادر کیمیاگر هستند. آن‌ها برای زنده کردن مادرشان ممنوعه‌ترین کار کیمیاگری، یعنی تبدیل انسان، را انجام دادند "
  "و ادوارد دست و پا و آلفونس تمام بدنش را از دست داد. حالا دنبال «سنگ فیلسوف» می‌گردند تا بدن‌هایشان را برگردانند."),
 ("شیطان‌کش Demon Slayer - کیمتسو نو یایبا",
  "شیطان‌کش (Demon Slayer یا Kimetsu no Yaiba) از روی مانگای کویوهارو گوتوگه ساخته شده و استودیو یوفوتیبل (ufotable) آن را از سال ۲۰۱۹ می‌سازد. "
  "فیلم «قطار بی‌پایان» (Mugen Train) در سال ۲۰۲۰ فروش بسیار بالایی داشت و این انیمه به‌خاطر انیمیشن‌های زیبایش مشهور است.\n\n"
  "داستان: تانجیرو کامادو خانواده‌اش را در حمله‌ی دیوها از دست می‌دهد و خواهرش نزوکو به دیو تبدیل می‌شود. او به سپاه شیطان‌کش‌ها می‌پیوندد "
  "تا دیوها را شکست دهد و راهی برای برگرداندن خواهرش به انسان پیدا کند."),
 ("آکادمی قهرمان من My Hero Academia - بوکو نو هیرو آکادمیا",
  "آکادمی قهرمان من (My Hero Academia یا Boku no Hero Academia) از روی مانگای کوهی هوریکوشی ساخته شده و استودیو بونز آن را از سال ۲۰۱۶ می‌سازد.\n\n"
  "داستان: در دنیایی که بیشتر مردم «کویرک» یا توانایی ویژه دارند، ایزوکو میدوریا (دکو) بدون توانایی به دنیا آمده ولی آرزو دارد قهرمان شود. "
  "قهرمان بزرگ، آل‌مایت، توانایی «وان فور آل» را به او می‌دهد و او در دبیرستان قهرمان‌پروری UA درس می‌خواند. "
  "از شخصیت‌های دیگر کاتسوکی باکوگو و شوتو تودوروکی هستند."),
 ("دراگون بال زد Dragon Ball Z",
  "دراگون بال زد (Dragon Ball Z) ادامه‌ی دراگون بال و از روی مانگای آکیرا توریاما است. استودیو توئی (Toei Animation) آن را بین سال‌های ۱۹۸۹ تا ۱۹۹۶ ساخت.\n\n"
  "داستان: گوکو یک ساین (جنگجوی فضایی) است که روی زمین بزرگ شده و از زمین در برابر دشمنان قدرتمند محافظت می‌کند. "
  "دشمنان اصلی: فریزا، سل و ماجین بو. از شخصیت‌های دیگر وجیتا و گوهان (پسر گوکو) هستند. با جمع کردن هفت «دراگون بال» می‌شود آرزو کرد "
  "و تبدیل شدن به «سوپر ساین» از معروف‌ترین لحظه‌های این انیمه است."),
 ("هانتر × هانتر Hunter x Hunter",
  "هانتر × هانتر (Hunter x Hunter) از روی مانگای یوشیهیرو توگاشی است. نسخه‌ی ۲۰۱۱ آن را استودیو مدهاوس ساخته و ۱۴۸ قسمت دارد.\n\n"
  "داستان: گان فریکس پسر نوجوانی است که می‌خواهد «هانتر» شود تا پدرش، جینگ، را پیدا کند. او در آزمون هانتر با کیلوا، کورپیکا و لئوریو دوست می‌شود. "
  "سیستم قدرت این دنیا «نن» (Nen) نام دارد و داستان به‌خاطر تدبیر و استراتژی‌اش معروف است."),
 ("جوجوتسو کایسن Jujutsu Kaisen",
  "جوجوتسو کایسن (Jujutsu Kaisen) از روی مانگای گگه آکوتامی است و استودیو مپا (MAPPA) آن را از سال ۲۰۲۰ می‌سازد.\n\n"
  "داستان: یوجی ایتادوری، دانش‌آموزی با توان بدنی خارق‌العاده، انگشت نفرین‌شده‌ی «سوکونا» (پادشاه نفرین‌ها) را می‌بلعد و سوکونا درون بدن او ساکن می‌شود. "
  "او به دبیرستان جوجوتسوی توکیو می‌رود و با مگومی فوشیگورو، نوبارا کوگیساکی و معلم قدرتمندشان ساتورو گوجو با نفرین‌ها می‌جنگد."),
]

def seed_anime_pack():
    c = db()
    exists = c.execute("SELECT 1 FROM packs WHERE id=?", (ANIME_PACK_ID,)).fetchone()
    if not exists:
        c.execute("INSERT INTO packs(id,name,desc,url,kind,status,enabled,progress,msg,custom) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (ANIME_PACK_ID, ANIME_PACK_NAME, "اطلاعات ۱۰ انیمه‌ی معروف: داستان، شخصیت‌ها و سازندگان", "", "text",
                   "ready", 1, 1, "آماده (۱۰ انیمه)", 1))
    delete_chunks(c, ANIME_PACK_ID)
    for title, text in ANIME_DOCS:
        add_chunks(c, ANIME_PACK_ID, title, text, size=3000)
    c.execute("UPDATE packs SET name=?, status='ready', progress=1, msg='آماده (۱۰ انیمه)' WHERE id=?",
              (ANIME_PACK_NAME, ANIME_PACK_ID))
    if not exists:
        c.execute("UPDATE packs SET enabled=1 WHERE id=?", (ANIME_PACK_ID,))
    c.commit(); c.close()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/packs":
            self._json({"packs": list_packs(), "llm": llm_model()})
        elif self.path == "/api/profile":
            self._json(get_profile())
        else:
            self.send_error(404)

    def do_POST(self):
        p = self.path
        if p == "/api/download":
            pid = json.loads(self._body())["id"]
            threading.Thread(target=download_pack, args=(pid,), daemon=True).start()
            self._json({"ok": True})
        elif p == "/api/toggle":
            d = json.loads(self._body())
            set_pack(d["id"], enabled=1 if d["enabled"] else 0)
            self._json({"ok": True})
        elif p == "/api/delete":
            pid = json.loads(self._body())["id"]
            with _db_lock:
                c = db()
                delete_chunks(c, pid)
                if c.execute("SELECT custom FROM packs WHERE id=?", (pid,)).fetchone()["custom"]:
                    c.execute("DELETE FROM packs WHERE id=?", (pid,))
                else:
                    c.execute("UPDATE packs SET status='none',enabled=0,progress=0,msg='' WHERE id=?", (pid,))
                c.commit(); c.close()
            self._json({"ok": True})
        elif p.startswith("/api/upload/"):
            fname = unquote(p.split("/api/upload/", 1)[1]) or "file.txt"
            pid = "my_" + re.sub(r"\W+", "_", fname)[:40] + str(int(time.time()) % 100000)
            path = os.path.join(PACKS_DIR, pid + ".txt")
            with open(path, "wb") as f:
                f.write(self._body())
            c = db()
            c.execute("INSERT INTO packs(id,name,desc,url,kind,custom) VALUES(?,?,?,?,?,1)",
                      (pid, fname, "فایل شخصی", "", "text"))
            c.commit(); c.close()
            index_pack(pid, path)
            set_pack(pid, enabled=1)
            self._json({"ok": True})
        elif p == "/api/profile":
            try:
                data = json.loads(self._body())
                save_profile(data)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif p == "/api/transcribe":
            try:
                audio = self._body()
                if not audio:
                    self._json({"error": "صدایی دریافت نشد."}, 400); return
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    self._json({"error": "ابزار ویس به متن نصب نیست. از منو ← کتابخانه ← «ابزار ویس به متن» را دانلود کن."}, 503); return
                model_path = os.environ.get("WHISPER_MODEL", "")
                if not model_path:
                    for name in ("whisper-small", "whisper-tiny"):
                        cand = os.path.join(MODELS_DIR, name)
                        if os.path.isdir(cand): model_path = cand; break
                if not model_path or not os.path.isdir(model_path):
                    self._json({"error": "مدل Whisper نصب نشده. از منو ← کتابخانه ← «ابزار ویس به متن» را دانلود کن."}, 503); return
                tmp = tempfile.NamedTemporaryFile(suffix=".webm", delete=False, dir=DATA)
                tmp.write(audio); tmp.close()
                try:
                    with _voice_lock:
                        model = _get_whisper(model_path)
                        segments, _ = model.transcribe(tmp.name, language="fa", vad_filter=True)
                        transcript = " ".join(seg.text.strip() for seg in segments).strip()
                finally:
                    try: os.remove(tmp.name)
                    except OSError: pass
                self._json({"text": transcript})
            except Exception as e:
                self._json({"error": f"خطا در تبدیل ویس: {e}"}, 500)
        elif p == "/api/tts":
            try:
                d = json.loads(self._body())
                text = str(d.get("text", "")).strip()
                if not text:
                    self._json({"error": "متنی برای خواندن وجود ندارد."}, 400); return
                try:
                    import pyttsx3
                except ImportError:
                    self._json({"error": "ابزار صدا نصب نیست. از منو ← کتابخانه ← «ابزار صدای پاسخ» را دانلود کن."}, 503); return
                out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=DATA)
                out.close()
                try:
                    with _voice_lock:
                        engine = pyttsx3.init()
                        engine.save_to_file(text, out.name)
                        engine.runAndWait()
                        engine.stop()
                    with open(out.name, "rb") as f:
                        audio = f.read()
                finally:
                    try: os.remove(out.name)
                    except OSError: pass
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Content-Disposition", "attachment; filename=assistant-voice.wav")
                self.end_headers()
                self.wfile.write(audio)
            except Exception as e:
                self._json({"error": f"خطا در ساخت صدا: {e}"}, 500)
        elif p == "/api/chat":
            self.chat(json.loads(self._body()))
        else:
            self.send_error(404)

    def chat(self, d):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        def send(o):
            self.wfile.write((json.dumps(o, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        q = d.get("question", "")
        try:
            res = search(q)
            if res is None:
                send({"t": "text", "v": "هیچ کتابخانه‌ی دانشی فعال نیست. از منوی کناری بخش کتابخانه را باز کن و یکی را دانلود و فعال کن."})
            elif not res:
                send({"t": "text", "v": "در کتابخانه‌های فعال چیزی درباره‌ی این سؤال پیدا نکردم."})
            else:
                send({"t": "sources", "v": [{"title": r["title"]} for r in res]})
                model = llm_model()
                profile = get_profile()
                system = build_system_prompt(profile)
                answered = False
                if model:
                  try:
                    ctx = "\n\n".join(f"[{i+1}] {r['title']}:\n{r['text']}" for i, r in enumerate(res))
                    msgs = [{"role": "system", "content": system + "\n\nمنابع:\n" + ctx}]
                    hist = [{"role": m.get("role"), "content": m.get("content", "")}
                            for m in d.get("history", [])[-6:] if m.get("role") in ("user", "assistant")]
                    msgs += hist + [{"role": "user", "content": q}]
                    for tok in llm_stream(model, msgs):
                        answered = True
                        send({"t": "text", "v": tok})
                  except (BrokenPipeError, ConnectionResetError):
                    raise
                  except Exception:
                    pass
                if not answered:
                    text = res[0]["text"].strip()
                    if "پاسخ:" in text:
                        text = text.split("پاسخ:", 1)[-1].strip()
                    send({"t": "text", "v": text})
        except Exception as e:
            send({"t": "text", "v": f"خطا: {e}"})
        send({"t": "done"})


HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#212121">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>دستیار آفلاین</title>
<style>
:root{
  --bg:#212121;--panel:#171717;
  --line:#3c3c3c;--text:#f5f5f5;--muted:#a1a1aa;--blue:#2563eb;--blue2:#1d4ed8;
  --safe-top:env(safe-area-inset-top,0px);
  --safe-bottom:env(safe-area-inset-bottom,0px);
  --safe-left:env(safe-area-inset-left,0px);
  --safe-right:env(safe-area-inset-right,0px);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;height:100dvh;font-family:Tahoma,Vazirmatn,Arial,sans-serif;background:var(--bg);color:var(--text);overflow:hidden}
button,textarea,input,select{font:inherit}
button{cursor:pointer;border:0;color:inherit;background:none}
.app{height:100%;height:100dvh;display:flex;min-width:0;position:relative;overflow:hidden;
  padding-top:var(--safe-top);padding-bottom:var(--safe-bottom);
  padding-left:var(--safe-left);padding-right:var(--safe-right)}

.side{position:fixed;top:0;right:0;bottom:0;width:min(88vw,320px);background:var(--panel);z-index:40;
  display:flex;flex-direction:column;transform:translateX(105%);transition:transform .22s ease;
  padding-top:var(--safe-top);padding-bottom:var(--safe-bottom);box-shadow:-8px 0 28px #0007;
  border-left:1px solid var(--line);overflow:hidden}
.side.open{transform:translateX(0)}
.overlay{display:none;position:fixed;inset:0;background:#0008;z-index:35}
.overlay.show{display:block}
.side-head{display:flex;align-items:center;justify-content:space-between;padding:14px 14px 12px;
  border-bottom:1px solid var(--line);flex-shrink:0}
.side-head .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px}
.logo{width:34px;height:34px;border-radius:11px;background:linear-gradient(145deg,#4285f4,#1d4ed8);
  display:grid;place-items:center;font-size:18px}
.side-close{width:38px;height:38px;border-radius:10px;font-size:20px;display:grid;place-items:center}
.side-close:hover{background:#333}

.menu-main{padding:14px 14px 0;flex-shrink:0}
.menu-btn{width:100%;display:flex;align-items:center;gap:12px;background:#252525;border:1px solid #3a3a3a;
  border-radius:12px;padding:14px;margin-bottom:10px;font-size:14px;text-align:right;transition:.15s}
.menu-btn:hover{background:#303030}
.menu-btn .ico{font-size:18px;width:28px;text-align:center}
.menu-btn .lbl{flex:1;font-weight:600}
.menu-btn .arr{color:#666;font-size:16px}
.side-divider{height:1px;background:var(--line);margin:4px 14px 10px;flex-shrink:0}

.chats-block{flex-shrink:0;padding:0 14px 8px;max-height:36%;overflow:auto}
.new-chat-btn{width:100%;background:#292929;border:1px solid #444;border-radius:12px;padding:12px;
  text-align:right;margin-bottom:8px;transition:.15s;font-size:13px}
.new-chat-btn:hover{background:#303030}
.section-title{font-size:12px;color:var(--muted);margin:6px 2px 6px}
.chat-item{display:flex;align-items:center;gap:8px;padding:10px;border-radius:11px;margin:3px 0;cursor:pointer;transition:.15s}
.chat-item:hover,.chat-item.active{background:#2a2a2a}
.chat-item .ci-title{flex:1;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item .ci-del{opacity:.45;font-size:13px;padding:4px 6px;border-radius:6px}
.chat-item .ci-del:hover{opacity:1;background:#444}
.empty-chats{color:#555;font-size:12px;padding:6px 4px}

.side-body{flex:1;overflow:auto;padding:0 14px 18px;min-height:0}
.sub-page{display:none}.sub-page.show{display:block}
.sub-back{display:flex;align-items:center;gap:8px;padding:8px 0 10px;font-size:14px;color:#ccc}
.sub-back button{font-size:18px;padding:6px 8px;border-radius:8px}
.sub-back button:hover{background:#333}
.form-group{margin:11px 0}
.form-group label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.form-group input,.form-group select,.form-group textarea{width:100%;background:#2a2a2a;border:1px solid #444;
  border-radius:10px;padding:10px 12px;color:var(--text);font-size:13px;outline:none}
.form-group textarea{min-height:100px;resize:vertical;line-height:1.7}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn-save{width:100%;background:var(--blue);color:#fff;border-radius:11px;padding:12px;font-size:14px;margin-top:10px}
.btn-save:hover{background:var(--blue2)}
.btn-gray{background:#3a3a3a;border-radius:9px;padding:8px 12px;font-size:12px}
.profile-hint{font-size:11px;color:var(--muted);line-height:1.7;margin:4px 0 8px}
.pack{background:#222;border:1px solid #393939;border-radius:12px;padding:11px;margin:8px 0;font-size:12px}
.pack b{font-size:13px}.pack small{display:block;color:var(--muted);line-height:1.8;margin:4px 0}
.bar{height:5px;background:#444;border-radius:8px;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:var(--blue)}
button.primary{background:var(--blue);border-radius:9px;padding:8px 12px;color:#fff;font-size:12px}
button.primary:hover{background:var(--blue2)}
.voice-box{background:#1e2a22;border:1px solid #2d4a38;border-radius:12px;padding:12px;margin:10px 0;font-size:12px}
.voice-box b{color:#7ddea8}

.main{flex:1;min-width:0;display:flex;flex-direction:column;position:relative;width:100%;overflow:hidden}
.top{height:54px;flex-shrink:0;border-bottom:1px solid #333;display:flex;align-items:center;justify-content:space-between;
  padding:0 12px;background:#212121ef;backdrop-filter:blur(12px)}
.top button{background:transparent;font-size:22px;padding:8px;border-radius:9px;width:40px;height:40px;display:grid;place-items:center}
.top button:hover{background:#333}
.top .title{font-size:14px;font-weight:600;max-width:50%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top small{color:#c4d7ff;background:#1e3a68;border:1px solid #31558d;border-radius:20px;padding:5px 10px;font-size:11px;
  max-width:36%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.scroll{flex:1;overflow-x:hidden;overflow-y:auto;scroll-behavior:smooth;-webkit-overflow-scrolling:touch}
.welcome{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px 16px}
.hero{width:58px;height:58px;border-radius:18px;background:linear-gradient(145deg,#4285f4,#1d4ed8);display:grid;place-items:center;
  font-size:26px;margin-bottom:14px;box-shadow:0 10px 28px #2563eb35}
.welcome h1{font-size:clamp(20px,4vw,26px);margin:0 0 10px;font-weight:650}
.welcome p{font-size:13px;color:var(--muted);margin:0 0 22px;line-height:1.8}
.ideas{width:min(540px,100%);display:grid;grid-template-columns:1fr 1fr;gap:10px}
.idea{background:#292929;border:1px solid #414141;border-radius:14px;padding:13px;text-align:right;font-size:13px;line-height:1.8;transition:.15s}
.idea:hover{background:#303030}.idea small{color:var(--muted)}

.messages{width:100%;max-width:860px;margin:0 auto;padding:14px 12px 20px;overflow-x:hidden}
.msg{display:flex;gap:8px;margin:14px 0;align-items:flex-start;animation:up .2s ease;max-width:100%}
@keyframes up{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.avatar{width:28px;height:28px;flex:0 0 28px;border-radius:50%;background:#343434;display:grid;place-items:center;font-size:13px}

/* کاربر سمت راست | دستیار سمت چپ — متن فارسی RTL دست‌نخورده */
.msg.user{justify-content:flex-start}
.msg.user .avatar{display:none}
.msg.user .body{flex:0 1 auto;max-width:min(78%,520px);background:var(--blue);border:1px solid #4c83f5;
  border-radius:18px 18px 4px 18px;padding:10px 14px;color:#fff;box-shadow:0 4px 14px #0002;
  text-align:right;direction:rtl;font-size:14px;line-height:1.9;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}

.msg.assistant{justify-content:flex-end}
.msg.assistant .body{flex:0 1 auto;max-width:min(85%,640px);text-align:right;direction:rtl;
  font-size:14px;line-height:1.95;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;padding:2px 4px}

.src{font-size:11px;color:var(--muted);border-top:1px solid #444;margin-top:10px;padding-top:7px}
.actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.actions button{font-size:11px;color:#bbb;background:#303030;border:1px solid #444;border-radius:8px;padding:6px 10px}
.actions button:hover{background:#414141}

.composer-area{flex-shrink:0;padding:8px 10px calc(10px + var(--safe-bottom));background:linear-gradient(180deg,transparent,#212121 20%)}
.composer{width:100%;max-width:860px;margin:0 auto;background:#2b2b2b;border:1px solid #484848;border-radius:22px;
  padding:8px 10px 7px;box-shadow:0 6px 24px #0003}
.composer:focus-within{border-color:#777}
#q{width:100%;min-height:38px;max-height:140px;resize:none;background:transparent;border:0;outline:0;color:var(--text);
  font-size:14px;padding:8px 6px;line-height:1.7}
#q::placeholder{color:#888}
.tools{display:flex;align-items:center;justify-content:space-between}
.tools button{background:transparent;color:#ccc;border-radius:10px;padding:7px 9px;font-size:12px}
.tools button:hover{background:#414141}
.tools .send{background:var(--blue);color:#fff;font-size:20px;width:36px;height:36px;padding:0;border-radius:50%;display:grid;place-items:center}
.tools .send:hover{background:var(--blue2)}
.tools .recording{background:#9b3333;color:#fff}
.hint{text-align:center;color:#666;font-size:10px;margin-top:7px}
.toast{display:none;position:fixed;z-index:50;bottom:calc(90px + var(--safe-bottom));left:50%;transform:translateX(-50%);
  background:#3a3a3a;border:1px solid #555;border-radius:11px;padding:11px 15px;font-size:12px;max-width:90%;box-shadow:0 8px 25px #0006}

@media(min-width:900px){
  .side{position:relative;transform:none;width:300px;flex:0 0 300px;box-shadow:none}
  .overlay{display:none!important}.side-close{display:none}.app{padding:0}
}
@media(max-width:760px){
  .ideas{grid-template-columns:1fr}
  .msg.user .body{max-width:82%;font-size:13px}
  .msg.assistant .body{max-width:88%;font-size:13px}
  .messages{padding:12px 10px 16px}
}
</style>
</head>
<body>
<div class="app">
  <aside class="side" id="side">
    <div class="side-head">
      <div class="brand"><div class="logo">✦</div>دستیار هوشمند</div>
      <button class="side-close" onclick="closeSide()" aria-label="بستن">✕</button>
    </div>

    <div class="menu-main" id="menuMain">
      <button class="menu-btn" onclick="openAccount()">
        <span class="ico">👤</span><span class="lbl">اکانت</span><span class="arr">‹</span>
      </button>
      <button class="menu-btn" onclick="openLibrary()">
        <span class="ico">📚</span><span class="lbl">کتابخانه</span><span class="arr">‹</span>
      </button>
    </div>

    <div class="side-divider"></div>

    <div class="chats-block" id="chatsBlock">
      <button class="new-chat-btn" onclick="newChat()">＋　گفت‌وگوی جدید</button>
      <div class="section-title">گفت‌وگوهای قبلی</div>
      <div id="chatList"></div>
    </div>

    <div class="side-body">
      <div class="sub-page" id="accountPage">
        <div class="sub-back"><button onclick="backToMenu()">→</button><span>اکانت</span></div>
        <div class="profile-hint">اطلاعات شخصی (اختیاری). هنگام پاسخ‌گویی به این‌ها توجه می‌شود.</div>
        <div class="form-row">
          <div class="form-group"><label>نام</label><input id="pfFirst" placeholder="مثلاً سارا"></div>
          <div class="form-group"><label>نام خانوادگی</label><input id="pfLast" placeholder="اختیاری"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>سن</label><input id="pfAge" type="number" min="1" max="120" placeholder="مثلاً ۱۳"></div>
          <div class="form-group"><label>جنسیت</label>
            <select id="pfGender"><option value="">—</option><option>دختر</option><option>پسر</option><option>زن</option><option>مرد</option><option>سایر</option></select>
          </div>
        </div>
        <div class="form-group"><label>رنگ مورد علاقه</label><input id="pfColor" placeholder="مثلاً آبی"></div>
        <div class="form-group"><label>درباره خودت / علایق</label>
          <textarea id="pfBio" placeholder="علایق، حساسیت‌ها، شرایط سلامتی و ..."></textarea>
        </div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <button class="btn-gray" style="flex:1" onclick="$('bioFile').click()">آپلود فایل متنی</button>
          <input id="bioFile" type="file" accept=".txt,.md" hidden>
        </div>
        <button class="btn-save" onclick="saveProfile()">ذخیره پروفایل</button>
      </div>

      <div class="sub-page" id="libraryPage">
        <div class="sub-back"><button onclick="backToMenu()">→</button><span>کتابخانه</span></div>
        <div class="section-title">بسته‌های دانش</div>
        <div id="packs"></div>
        <div class="pack">
          <b>افزودن فایل شخصی</b>
          <small>فایل‌های TXT و MD را وارد کن تا به کتابخانه اضافه شود.</small>
          <button class="btn-gray" onclick="$('up').click()" style="margin-top:6px">انتخاب فایل</button>
          <input id="up" type="file" accept=".txt,.md" hidden>
        </div>
        <div class="voice-box">
          <b>🎙 تبدیل صوت به متن (آفلاین)</b>
          <small style="display:block;margin:6px 0;color:#a0b8a8;line-height:1.7">
            از دکمه 🎙 ویس پایین صفحه چت استفاده کن.
            ابزار ویس به متن و ابزار صدای پاسخ را در همین لیست «بسته‌های دانش» دانلود کن.
          </small>
        </div>
      </div>
    </div>
  </aside>
  <div class="overlay" id="overlay" onclick="closeSide()"></div>

  <main class="main">
    <header class="top">
      <div style="display:flex;align-items:center;gap:6px">
        <button onclick="toggleSide()" aria-label="منو">☰</button>
        <span class="title" id="title">گفت‌وگوی جدید</span>
      </div>
      <small id="status">● آماده</small>
    </header>

    <div class="scroll" id="scroll">
      <div class="messages" id="msgs">
        <div class="welcome" id="welcome">
          <div class="hero">✦</div>
          <h1>امروز چه چیزی یاد بگیریم؟</h1>
          <p>سؤالت را بپرس؛ از کتابخانه دانشت برای پیدا کردن پاسخ استفاده می‌کنم.</p>
          <div class="ideas">
            <button class="idea" onclick="promptQ('خودت را معرفی کن')">✧ خودت را معرفی کن<br><small>با دستیار آشنا شو</small></button>
            <button class="idea" onclick="promptQ('یک موضوع علمی جالب توضیح بده')">◈ دانستنی علمی<br><small>چیزی تازه کشف کن</small></button>
            <button class="idea" onclick="promptQ('چطور بهتر یاد بگیرم؟')">⌁ یادگیری بهتر<br><small>ایده‌هایی برای شروع</small></button>
            <button class="idea" onclick="promptQ('چند ایده خلاقانه پیشنهاد بده')">✦ ایده‌پردازی<br><small>از یک فکر تازه شروع کن</small></button>
          </div>
        </div>
      </div>
    </div>

    <div class="composer-area">
      <div class="composer">
        <textarea id="q" rows="1" placeholder="پیامت را بنویس..."></textarea>
        <div class="tools">
          <div>
            <button type="button" onclick="$('up').click()">＋ فایل</button>
            <button type="button" id="mic" onclick="toggleRecord()">🎙 ویس</button>
            <span id="recLabel" style="display:none;color:#f99;font-size:11px">در حال ضبط...</span>
          </div>
          <button type="button" class="send" onclick="ask()" aria-label="ارسال">↑</button>
        </div>
      </div>
      <div class="hint">Enter برای ارسال · Shift+Enter خط جدید</div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let hist=[], busy=false, recorder=null, recChunks=[], recording=false;
let chats=JSON.parse(localStorage.getItem('oa_chats')||'[]');
let currentChatId=null;

function toast(t){$('toast').textContent=t;$('toast').style.display='block';clearTimeout(window._tt);window._tt=setTimeout(()=>$('toast').style.display='none',3500)}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function openSide(){$('side').classList.add('open');$('overlay').classList.add('show')}
function closeSide(){$('side').classList.remove('open');$('overlay').classList.remove('show')}
function toggleSide(){if($('side').classList.contains('open'))closeSide();else{backToMenu();openSide()}}

function backToMenu(){
  $('accountPage').classList.remove('show');
  $('libraryPage').classList.remove('show');
  $('menuMain').style.display='block';
  $('chatsBlock').style.display='block';
}
function openAccount(){
  $('menuMain').style.display='none';
  $('chatsBlock').style.display='none';
  $('libraryPage').classList.remove('show');
  $('accountPage').classList.add('show');
  loadProfile();
}
function openLibrary(){
  $('menuMain').style.display='none';
  $('chatsBlock').style.display='none';
  $('accountPage').classList.remove('show');
  $('libraryPage').classList.add('show');
  loadPacks();
}

async function loadProfile(){
  try{const p=await(await fetch('/api/profile')).json();
    $('pfFirst').value=p.first_name||'';$('pfLast').value=p.last_name||'';
    $('pfAge').value=p.age||'';$('pfGender').value=p.gender||'';
    $('pfColor').value=p.favorite_color||'';$('pfBio').value=p.bio||'';
  }catch(e){}
}
async function saveProfile(){
  const data={first_name:$('pfFirst').value.trim(),last_name:$('pfLast').value.trim(),age:$('pfAge').value.trim(),
    gender:$('pfGender').value,favorite_color:$('pfColor').value.trim(),bio:$('pfBio').value.trim()};
  try{await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});toast('پروفایل ذخیره شد')}
  catch(e){toast('خطا در ذخیره')}
}
$('bioFile').onchange=async e=>{const f=e.target.files[0];if(!f)return;const t=await f.text();$('pfBio').value=($('pfBio').value+'\n'+t).trim();toast('متن فایل اضافه شد');e.target.value=''};

function saveChats(){try{localStorage.setItem('oa_chats',JSON.stringify(chats.slice(0,40)))}catch(e){}}
// گفت‌وگوهای خالی (بدون پیام) هیچ‌وقت ذخیره نمی‌شوند
chats=chats.filter(c=>c&&c.messages&&c.messages.length);saveChats();
function curChat(){return chats.find(c=>c.id===currentChatId)}
function showWelcome(){
  $('msgs').innerHTML='';
  const w=$('welcome');if(w){$('msgs').appendChild(w);w.style.display='flex'}
}
function renderChatList(){
  const list=$('chatList');
  if(!chats.length){list.innerHTML='<div class="empty-chats">هنوز گفت‌وگویی نیست</div>';return}
  list.innerHTML=chats.map(c=>`<div class="chat-item ${c.id===currentChatId?'active':''}" onclick="loadChat('${c.id}')">
    <div class="ci-title">${esc(c.title||'گفت‌وگوی بدون عنوان')}</div>
    <button class="ci-del" onclick="event.stopPropagation();delChat('${c.id}')">🗑</button></div>`).join('');
}
// گفت‌وگوی جدید: فقط صفحه‌ی خالی؛ وقتی اولین پیام فرستاده شد ساخته و ذخیره می‌شود
function newChat(){
  if(busy){toast('صبر کن تا پاسخ کامل شود');return}
  currentChatId=null;hist=[];
  showWelcome();
  $('title').textContent='گفت‌وگوی جدید';
  renderChatList();closeSide();$('q').focus();
}
function loadChat(id){
  if(busy){toast('صبر کن تا پاسخ کامل شود');return}
  const c=chats.find(x=>x.id===id);if(!c)return;
  currentChatId=id;hist=c.messages;$('msgs').innerHTML='';
  hist.forEach(m=>{
    if(m.role==='user')addMsg('user',m.content);
    else{const el=addMsg('assistant',m.content);
      if(m.src&&m.src.length){const z=document.createElement('div');z.className='src';z.textContent='منابع: '+m.src.join('، ');el.body.append(z)}
      actions(el.body,m.content)}
  });
  $('title').textContent=c.title||'گفت‌وگو';renderChatList();closeSide();bottom();
}
function delChat(id){
  if(busy)return;
  if(!confirm('این گفت‌وگو حذف شود؟'))return;
  chats=chats.filter(c=>c.id!==id);saveChats();
  if(currentChatId===id)newChat();else renderChatList();
}
// عنوان فقط یک بار از پیام اول انتخاب می‌شود
function makeTitle(q){
  const words=q.replace(/\s+/g,' ').trim();
  let t=words.slice(0,32);if(words.length>32)t=t.replace(/\s+\S*$/,'')+'…';
  return t||'گفت‌وگو';
}
function ensureChat(q){
  let c=curChat();
  if(!c){
    c={id:'c'+Date.now(),title:makeTitle(q),messages:[],created:Date.now()};
    chats.unshift(c);currentChatId=c.id;hist=c.messages;
    $('title').textContent=c.title;
  }
  return c;
}

async function api(path,body){return(await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json()}
async function loadPacks(){
  try{
    const r=await(await fetch('/api/packs')).json();
    $('status').textContent=r.llm?('● '+r.llm):'● حالت کتابخانه';
    $('packs').innerHTML=r.packs.map(p=>{
      let a='';
      if(p.status==='ready'&&p.kind==='tool')a='<span>نصب شده ✓</span>';
      else if(p.status==='ready')a=`<label style="margin-left:8px"><input type="checkbox" ${p.enabled?'checked':''} onchange="api('/api/toggle',{id:'${p.id}',enabled:this.checked})"> فعال</label> <button class="btn-gray" onclick="delPack('${p.id}')">حذف</button>`;
      else if(p.status==='none')a=`<button class="primary" onclick="dl('${p.id}')">دانلود</button>`;
      else a=`<div class="bar"><i style="width:${(p.progress||0)*100}%"></i></div>`;
      return `<div class="pack"><b>${esc(p.name)}</b><small>${esc(p.desc||'')}</small>${a}<small>${esc(p.msg||'')}</small></div>`;
    }).join('');
    if(r.packs.some(p=>['downloading','indexing'].includes(p.status)))setTimeout(loadPacks,1500);
  }catch(e){toast('سرور در دسترس نیست')}
}
async function dl(id){await api('/api/download',{id});loadPacks()}
async function delPack(id){if(confirm('حذف شود؟')){await api('/api/delete',{id});loadPacks()}}
$('up').onchange=async e=>{const f=e.target.files[0];if(!f)return;toast('در حال افزودن فایل...');try{const r=await fetch('/api/upload/'+encodeURIComponent(f.name),{method:'POST',body:await f.arrayBuffer()});toast(r.ok?'فایل اضافه شد':'خطا')}catch(err){toast('خطا')}loadPacks();e.target.value=''};

function addMsg(role,text){
  const wEl=$('welcome');if(wEl)wEl.style.display='none';
  const w=document.createElement('div');w.className='msg '+role;
  const a=document.createElement('div');a.className='avatar';a.textContent=role==='user'?'ش':'✦';
  const b=document.createElement('div');b.className='body';b.textContent=text;
  w.append(a,b);$('msgs').append(w);bottom();return{wrap:w,body:b};
}
function bottom(){$('scroll').scrollTop=$('scroll').scrollHeight}
function actions(el,text){
  const a=document.createElement('div');a.className='actions';
  const c=document.createElement('button');c.textContent='کپی پاسخ';c.onclick=()=>navigator.clipboard?.writeText(text).then(()=>toast('کپی شد'));
  const v=document.createElement('button');v.textContent='🔊 دانلود ویس پاسخ';v.onclick=()=>downloadVoice(text,v);
  a.append(c,v);el.append(a);
}
async function downloadVoice(text,btn){
  btn.disabled=true;btn.textContent='در حال ساخت...';
  try{
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    if(!r.ok){let e={};try{e=await r.json()}catch(_){}throw Error(e.error||'موتور صدا در دسترس نیست')}
    const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download='assistant-voice.wav';a.click();URL.revokeObjectURL(url);btn.textContent='✓ دانلود شد';
  }catch(e){toast(e.message);btn.textContent='🔊 دانلود ویس پاسخ'}finally{btn.disabled=false}
}
function promptQ(t){$('q').value=t;ask()}

async function ask(){
  if(busy)return;
  const q=$('q').value.trim();if(!q)return;
  $('q').value='';$('q').style.height='38px';
  const chat=ensureChat(q);
  const history=chat.messages.map(m=>({role:m.role,content:m.content}));
  addMsg('user',q);
  const m=addMsg('assistant','در حال فکر کردن...');
  let ans='',first=true,srcs=[];busy=true;$('status').textContent='● در حال پاسخ...';
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,history})});
    if(!r.ok)throw new Error('خطای سرور ('+r.status+')');
    const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
    while(true){
      const {done,value}=await rd.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n'))>=0){
        const line=buf.slice(0,i);buf=buf.slice(i+1);if(!line.trim())continue;
        let o;try{o=JSON.parse(line)}catch(_){continue}
        if(o.t==='text'){if(first){m.body.textContent='';first=false}ans+=o.v;m.body.textContent=ans;bottom()}
        if(o.t==='sources'){srcs=(o.v||[]).map(x=>x.title);const z=document.createElement('div');z.className='src';z.textContent='منابع: '+srcs.join('، ');m.body.append(z)}
      }
    }
    if(!ans)ans='پاسخی دریافت نشد.';
    if(first)m.body.textContent=ans;
    chat.messages.push({role:'user',content:q},{role:'assistant',content:ans,src:srcs});
    chat.messages=chat.messages.slice(-60);hist=chat.messages;
    chats=[chat,...chats.filter(x=>x.id!==chat.id)];
    saveChats();renderChatList();actions(m.body,ans);
  }catch(e){
    console.error(e);
    const offline=(e instanceof TypeError);
    m.body.textContent=offline?'ارتباط با برنامه قطع است. پنجره‌ی پایتون را چک کن (باید باز و در حال اجرا باشد) و صفحه را دوباره باز کن.':'خطا در دریافت پاسخ: '+(e.message||e);
    // پیام ناموفق هم در همان گفت‌وگو ذخیره می‌شود تا گفت‌وگوی جدید ساخته نشود
    chat.messages.push({role:'user',content:q});chats=[chat,...chats.filter(x=>x.id!==chat.id)];saveChats();renderChatList();
    toast(offline?'سرور در دسترس نیست':'خطا');
  }finally{busy=false;$('status').textContent='● آماده';bottom()}
}

async function toggleRecord(){
  if(recording){recorder.stop();return}
  if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){toast('ضبط صدا پشتیبانی نمی‌شود');return}
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    recChunks=[];recorder=new MediaRecorder(stream);
    recorder.ondataavailable=e=>{if(e.data.size)recChunks.push(e.data)};
    recorder.onstop=async()=>{
      recording=false;stream.getTracks().forEach(t=>t.stop());
      $('mic').classList.remove('recording');$('mic').textContent='🎙 ویس';$('recLabel').style.display='none';
      if(!recChunks.length)return;toast('در حال تبدیل ویس به متن...');
      try{
        const blob=new Blob(recChunks,{type:recorder.mimeType||'audio/webm'});
        const r=await fetch('/api/transcribe',{method:'POST',headers:{'Content-Type':blob.type},body:blob});
        const d=await r.json();if(!r.ok)throw Error(d.error||'ناموفق');
        if(d.text){$('q').value=($('q').value+' '+d.text).trim();$('q').dispatchEvent(new Event('input'));$('q').focus();toast('ویس تبدیل شد')}
        else toast('صدای واضحی تشخیص داده نشد');
      }catch(e){toast(e.message)}
    };
    recorder.start();recording=true;$('mic').classList.add('recording');$('mic').textContent='■ توقف';
    $('recLabel').style.display='inline';toast('ضبط شروع شد');
  }catch(e){toast('دسترسی میکروفون داده نشد: '+e.message)}
}

$('q').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask()}});
$('q').addEventListener('input',()=>{$('q').style.height='38px';$('q').style.height=Math.min($('q').scrollHeight,140)+'px'});

showWelcome();renderChatList();loadPacks();
</script>
</body>
</html>"""

def serve():
    host = os.environ.get("HOST", "127.0.0.1")
    servers = [ThreadingHTTPServer((host, PORT), H)]
    if host == "127.0.0.1":
        # «localhost» روی بعضی سیستم‌ها به ::1 می‌رود؛ همان‌جا هم گوش بده تا Failed to fetch نگیری
        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        try:
            servers.append(V6(("::1", PORT), H))
        except OSError:
            pass
    for sv in servers[1:]:
        threading.Thread(target=sv.serve_forever, daemon=True).start()
    print(f"باز کن: http://127.0.0.1:{PORT}")
    servers[0].serve_forever()

if __name__ == "__main__":
    init_db()
    seed_anime_pack()
    serve()
