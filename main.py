import urllib.parse, logging, sqlite3, threading, hashlib, os, pathlib, asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ── CONFIG ────────────────────────────────────────────────
TOKEN        = os.environ.get("TOKEN", "8616226817:AAHR-JpgZCl5c-6SbJsZoWcmhOONVCi2CaU")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "648488426"))
WEBSITE_BASE = "https://job-hire-india-web-production.up.railway.app"
JSEARCH_KEY  = "2b6a0da0dcmshe4ac8f02a58e471p11e577jsn12b21548bb48"
JOBS_PER_PAGE = 10

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# ── JOB CACHE SYSTEM ──────────────────────────────────────
# Jobs are fetched once every 6 hours and cached
# All users read from cache — saves API quota!
import time
_cache_store = {}  # {cache_key: {"jobs": [], "timestamp": time}}
CACHE_TTL    = 6 * 60 * 60  # 6 hours in seconds

def get_cached_jobs(keyword, state):
    """Get jobs from cache if fresh, else fetch new"""
    cache_key = f"{keyword}|{state}".lower()
    now       = time.time()

    # Return cached jobs if still fresh
    if cache_key in _cache_store:
        cached = _cache_store[cache_key]
        age    = now - cached["timestamp"]
        if age < CACHE_TTL:
            remaining = int((CACHE_TTL - age) / 3600)
            logging.info(f"Cache HIT for '{cache_key}' — {len(cached['jobs'])} jobs ({remaining}h remaining)")
            return cached["jobs"]
        else:
            logging.info(f"Cache EXPIRED for '{cache_key}' — fetching fresh jobs...")

    # Fetch fresh jobs
    logging.info(f"Cache MISS for '{cache_key}' — fetching from API...")
    jobs = fetch_live_jobs_from_api(keyword, state)

    # Store in cache
    _cache_store[cache_key] = {"jobs": jobs, "timestamp": now}
    logging.info(f"Cached {len(jobs)} jobs for '{cache_key}'")
    return jobs

def get_cache_status():
    """Get cache info for admin"""
    now    = time.time()
    lines  = []
    for key, val in _cache_store.items():
        age  = int((now - val["timestamp"]) / 60)
        left = max(0, int((CACHE_TTL - (now - val["timestamp"])) / 60))
        lines.append(f"• {key}: {len(val['jobs'])} jobs | age {age}m | refresh in {left}m")
    return "\n".join(lines) if lines else "No cache yet"

# ── DATABASE ──────────────────────────────────────────────
DB_PATH = str(pathlib.Path(__file__).parent / "jobbot.db")
_local  = threading.local()

def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn

def init_db():
    get_conn().executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, company TEXT NOT NULL,
        location TEXT DEFAULT '', category TEXT DEFAULT '',
        link TEXT DEFAULT '', source TEXT DEFAULT '',
        hash TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );""")
    get_conn().commit()
    logging.info("Database initialised ✅")

def add_user(uid):
    c = get_conn(); c.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,)); c.commit()

def get_all_users():
    return [r["id"] for r in get_conn().execute("SELECT id FROM users").fetchall()]

def user_count():
    return get_conn().execute("SELECT COUNT(*) FROM users").fetchone()[0]

def job_count():
    return get_conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

def get_latest_jobs(n=5):
    return get_conn().execute(
        "SELECT title,company,location,link FROM jobs ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()

def make_hash(*parts):
    combined = "|".join(p.lower().strip() for p in parts)
    return hashlib.sha256(combined.encode()).hexdigest()

def upsert_job(title, company, location, category, link, source):
    conn = get_conn()
    h = make_hash(title, company)
    if conn.execute("SELECT id FROM jobs WHERE hash=?", (h,)).fetchone():
        return False
    conn.execute(
        "INSERT INTO jobs(title,company,location,category,link,source,hash) VALUES(?,?,?,?,?,?,?)",
        (title, company, location, category, link, source, h)
    )
    conn.commit()
    return True

def search_db(keyword):
    k = f"%{keyword}%"
    return get_conn().execute(
        "SELECT title,company,location,link FROM jobs WHERE title LIKE ? OR company LIKE ?",
        (k, k)
    ).fetchall()

# ── JOB CACHE (store fetched jobs per user session) ───────
_job_cache = {}  # {user_id: {"jobs": [], "cat": "", "state": "", "keyword": ""}}

# ── CATEGORY MAPPINGS ─────────────────────────────────────
CAT_TYPE = {
    "💻 Software Developer":"IT","🤖 AI / Data Jobs":"IT",
    "🎨 UI/UX Designer":"IT","🎬 Video Editor":"IT",
    "📞 Customer Support":"IT","📈 Marketing":"IT",
    "📊 Sales Jobs":"IT","✍ Content Writer":"IT",
    "🏗 Construction":"IT","⚡ Electrician":"IT",
    "🚚 Delivery Driver":"IT","🧰 Technician":"IT",
    "🎓 Teaching":"IT","🏥 Healthcare":"IT",
    "🏛 Government Jobs":"GOVT","👮 Police Jobs":"POLICE",
    "🚆 Railway Jobs":"RAILWAY","🏦 Bank Jobs":"BANK",
    "⚖ Court Jobs":"COURT",
}

CAT_DB_MAP = {
    "💻 Software Developer":"IT Jobs","🤖 AI / Data Jobs":"IT Jobs",
    "🎨 UI/UX Designer":"IT Jobs","🎬 Video Editor":"IT Jobs",
    "🏛 Government Jobs":"Govt Jobs","👮 Police Jobs":"Govt Jobs",
    "🚆 Railway Jobs":"Govt Jobs","🏦 Bank Jobs":"Govt Jobs",
    "⚖ Court Jobs":"Govt Jobs","📞 Customer Support":"IT Jobs",
    "📈 Marketing":"IT Jobs","📊 Sales Jobs":"IT Jobs",
    "✍ Content Writer":"IT Jobs","🏗 Construction":"IT Jobs",
    "⚡ Electrician":"IT Jobs","🚚 Delivery Driver":"IT Jobs",
    "🧰 Technician":"IT Jobs","🎓 Teaching":"Internships",
    "🏥 Healthcare":"IT Jobs",
}

LIVE_JOB_CATEGORIES = {
    "💻 Software Developer","🤖 AI / Data Jobs","🎨 UI/UX Designer",
    "🎬 Video Editor","📞 Customer Support","📈 Marketing",
    "📊 Sales Jobs","✍ Content Writer","🎓 Teaching","🏥 Healthcare",
    "🏛 Government Jobs","🏦 Bank Jobs","🚆 Railway Jobs",
}

# ── STATES & CATEGORIES ───────────────────────────────────
states = [
    "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh",
    "Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand",
    "Karnataka","Kerala","Madhya Pradesh","Maharashtra","Manipur",
    "Meghalaya","Mizoram","Nagaland","Odisha","Punjab",
    "Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura",
    "Uttar Pradesh","Uttarakhand","West Bengal"
]

categories = {
    "💻 Software Developer":"software developer","🤖 AI / Data Jobs":"data scientist",
    "🎨 UI/UX Designer":"ui ux designer","🎬 Video Editor":"video editor",
    "🏛 Government Jobs":"government jobs","👮 Police Jobs":"police jobs",
    "🚆 Railway Jobs":"railway jobs","🏦 Bank Jobs":"bank jobs",
    "⚖ Court Jobs":"court jobs","📞 Customer Support":"customer support",
    "📈 Marketing":"marketing jobs","📊 Sales Jobs":"sales jobs",
    "✍ Content Writer":"content writer","🏗 Construction":"construction jobs",
    "⚡ Electrician":"electrician jobs","🚚 Delivery Driver":"delivery driver",
    "🧰 Technician":"technician jobs","🎓 Teaching":"teaching jobs",
    "🏥 Healthcare":"healthcare jobs"
}

# ── STATE SITES ───────────────────────────────────────────
state_sites_all = {
"Telangana": {
    "IT":[("Hyderabad Jobs","https://www.hyderabadjobs.net"),("TSJobs","https://www.tsjobs.in"),("TelanganaCareers","https://www.telanganacareers.com"),("DEET Employment","https://deet.telangana.gov.in"),("NCS Telangana","https://www.ncs.gov.in"),("Apna Hyderabad","https://apna.co/jobs?location=Hyderabad")],
    "GOVT":[("TSPSC","https://tgpsc.gov.in"),("TS State Portal","https://www.telangana.gov.in"),("Telangana SarkariJobs","https://telangana.sarkarijobs.com"),("Telangana 20Govt","https://telangana.20govt.com"),("FreeJobAlert TS","https://www.freejobalert.com/telangana-government-jobs"),("TSPSC Direct","https://websitenew.tgpsc.gov.in/directRecruitment")],
    "POLICE":[("TS Police","https://www.tgprb.in"),("TS Police Official","https://www.tspolice.gov.in"),("FreeJobAlert TS Police","https://www.freejobalert.com/telangana-police-jobs")],
    "RAILWAY":[("RRB Secunderabad","https://www.rrbsecunderabad.gov.in"),("SCR Railway","https://scr.indianrailways.gov.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")],
    "BANK":[("IBPS","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")],
    "COURT":[("TS High Court","https://tshc.gov.in"),("Telangana District Courts","https://districts.ecourts.gov.in/telangana"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")],
},
"Andhra Pradesh": {
    "IT":[("APCareers","https://www.apcareers.in/state/andhra-pradesh"),("DEET AP","https://deet.ap.gov.in"),("NCS AP","https://www.ncs.gov.in")],
    "GOVT":[("APPSC","https://psc.ap.gov.in"),("APPSC Notifications","https://portal-psc.ap.gov.in/HomePages/RecruitmentNotifications"),("AP SarkariJobs","https://ap.sarkarijobs.com"),("AP 20Govt","https://andhra-pradesh.20govt.com"),("FreeJobAlert AP","https://www.freejobalert.com/ap-government-jobs"),("Adda247 AP","https://www.adda247.com/exams/andhra-pradesh")],
    "POLICE":[("AP Police","https://slprb.ap.gov.in"),("FreeJobAlert AP Police","https://www.freejobalert.com/andhra-pradesh-police-jobs")],
    "RAILWAY":[("RRB Vijayawada","https://www.rrbvijayawada.gov.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")],
    "BANK":[("IBPS","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")],
    "COURT":[("AP High Court","https://aphc.gov.in/recruitments.php"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")],
},
"Karnataka": {
    "IT":[("KarnatakaCareers","https://www.karnatakacareers.org"),("KarnatakaGovtJobs","https://karnatakagovtjobs.com"),("Bangalore Jobs","https://in.indeed.com/jobs?l=Bangalore"),("NCS Karnataka","https://www.ncs.gov.in")],
    "GOVT":[("KPSC","https://kpsc.kar.nic.in"),("Karnataka SarkariJobs","https://karnataka.sarkarijobs.com"),("Karnataka 20Govt","https://karnataka.20govt.com"),("FreeJobAlert Karnataka","https://www.freejobalert.com/karnataka-government-jobs")],
    "POLICE":[("Karnataka Police","https://ksp.karnataka.gov.in"),("FreeJobAlert KA Police","https://www.freejobalert.com/karnataka-police-jobs")],
    "RAILWAY":[("RRB Bangalore","https://www.rrbbnc.gov.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")],
    "BANK":[("IBPS","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")],
    "COURT":[("Karnataka High Court","https://karnatakajudiciary.kar.nic.in/recruitment"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")],
},
"Maharashtra": {
    "IT":[("MajhiNaukri","https://majhinaukri.in"),("eMahaJobs","https://emahajobs.com"),("Mumbai Jobs","https://in.indeed.com/jobs?l=Mumbai"),("NCS Maharashtra","https://www.ncs.gov.in")],
    "GOVT":[("MPSC","https://mpsc.gov.in"),("Maharashtra SarkariJobs","https://maharashtra.sarkarijobs.com"),("Maharashtra 20Govt","https://maharashtra.20govt.com"),("FreeJobAlert Maha","https://www.freejobalert.com/maharashtra-government-jobs")],
    "POLICE":[("Maharashtra Police","https://mahapolice.gov.in/recruitment"),("FreeJobAlert Maha Police","https://www.freejobalert.com/maharashtra-police-jobs")],
    "RAILWAY":[("RRB Mumbai","https://www.rrbmumbai.gov.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")],
    "BANK":[("IBPS","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")],
    "COURT":[("Bombay High Court","https://bombayhighcourt.nic.in/recruitment"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")],
},
"Uttar Pradesh": {
    "IT":[("SewaYojan","https://sewayojan.up.nic.in"),("Rojgaar Sangam","https://rojgaarsangam.up.gov.in"),("NCS UP","https://www.ncs.gov.in")],
    "GOVT":[("UPPSC","https://uppsc.up.nic.in"),("UPSSSC","https://upsssc.gov.in"),("UP SarkariJobs","https://uttar-pradesh.sarkarijobs.com"),("UP 20Govt","https://uttar-pradesh.20govt.com"),("FreeJobAlert UP","https://www.freejobalert.com/up-government-jobs"),("GovtJobGuru UP","https://govtjobguru.in/govt-jobs-state-wise/uttar-pradesh")],
    "POLICE":[("UP Police","https://uppbpb.gov.in"),("FreeJobAlert UP Police","https://www.freejobalert.com/up-police-jobs")],
    "RAILWAY":[("RRB Allahabad","https://www.rrbald.gov.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")],
    "BANK":[("IBPS","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")],
    "COURT":[("Allahabad High Court","https://www.allahabadhighcourt.in/recruitment"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")],
},
}

def get_default_state_portals(cat_name, state):
    cat_type  = CAT_TYPE.get(cat_name, "IT")
    state_slug = state.lower().replace(" ", "-")
    if cat_type == "IT":
        return [("NCS Portal","https://www.ncs.gov.in"),("Indeed",f"https://in.indeed.com/jobs?l={urllib.parse.quote(state)}"),("Apna",f"https://apna.co/jobs?location={urllib.parse.quote(state)}")]
    elif cat_type == "GOVT":
        return [("SarkariJobs",f"https://{state_slug}.sarkarijobs.com"),("20Govt",f"https://{state_slug}.20govt.com"),("FreeJobAlert",f"https://www.freejobalert.com/{state_slug}-government-jobs")]
    elif cat_type == "POLICE":
        return [("FreeJobAlert Police",f"https://www.freejobalert.com/{state_slug}-police-jobs"),("SarkariResult Police","https://www.sarkariresult.com/police")]
    elif cat_type == "RAILWAY":
        return [("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs"),("SarkariResult Railway","https://www.sarkariresult.com/railway")]
    elif cat_type == "BANK":
        return [("IBPS","https://www.ibps.in"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")]
    elif cat_type == "COURT":
        return [("FreeJobAlert Court","https://www.freejobalert.com/court-jobs"),("eCourts India","https://services.ecourts.gov.in")]
    return []

# ── HELPERS ───────────────────────────────────────────────
def make_redirect_link(site_name, job_url):
    return f"{WEBSITE_BASE}/redirect.html?site={urllib.parse.quote(site_name,safe='')}&url={urllib.parse.quote(job_url,safe='')}"

def fetch_live_jobs(keyword, state):
    """Public function — always use cache"""
    return get_cached_jobs(keyword, state)

def fetch_live_jobs_from_api(keyword, state):
    try:
        all_jobs = []
        url      = "https://jsearch.p.rapidapi.com/search"
        headers  = {"X-RapidAPI-Key": JSEARCH_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}

        # Fetch up to 5 pages (each page = 10 jobs = up to 50 jobs, faster!)
        for page in range(1, 6):
            params = {
                "query":       f"{keyword} jobs in {state} India",
                "page":        str(page),
                "num_pages":   "1",
                "country":     "in",
                "date_posted": "week"
            }
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            data = resp.json()
            jobs = data.get("data", [])
            if not jobs:
                break  # No more jobs available
            all_jobs.extend(jobs)
            logging.info(f"Fetched page {page}: {len(jobs)} jobs (total: {len(all_jobs)})")

        # If no state-specific jobs try all India
        if not all_jobs:
            logging.info(f"No jobs for {state}, trying all India...")
            for page in range(1, 6):
                params = {
                    "query":       f"{keyword} jobs India",
                    "page":        str(page),
                    "num_pages":   "1",
                    "country":     "in",
                    "date_posted": "week"
                }
                resp = requests.get(url, headers=headers, params=params, timeout=15)
                jobs = resp.json().get("data", [])
                if not jobs:
                    break
                all_jobs.extend(jobs)

        logging.info(f"Total jobs fetched: {len(all_jobs)}")
        return all_jobs
    except Exception as e:
        logging.error(f"JSearch error: {e}")
        return []

def format_jobs_page(jobs, page, total_pages, cat_name, state):
    start = page * JOBS_PER_PAGE
    end   = start + JOBS_PER_PAGE
    chunk = jobs[start:end]

    text = f"🔴 *LIVE JOB LISTINGS — Last 7 Days*\n📍 {cat_name} | {state}\n"
    text += f"📄 Page {page+1}/{total_pages}\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for i, job in enumerate(chunk, start+1):
        title    = job.get("job_title", "Job Opening")
        company  = job.get("employer_name", "Company")
        location = f"{job.get('job_city','') or ''} {job.get('job_state','') or ''}".strip() or state
        raw_link = job.get("job_apply_link", "")
        link     = make_redirect_link(company, raw_link) if raw_link else ""
        salary   = ""
        sal_min  = job.get("job_min_salary")
        sal_max  = job.get("job_max_salary")
        if sal_min and sal_max:
            salary = f"\n💰 ₹{int(sal_min):,} – ₹{int(sal_max):,}/yr"
        text += f"{i}. *{title}*\n🏢 {company}\n📍 {location}{salary}\n🔗 [Apply Now]({link})\n\n"

    return text

def jobs_nav_keyboard(page, total_pages, cat_name, state):
    buttons = []
    row     = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅ Previous", callback_data=f"page_{page-1}_{cat_name}|{state}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ➡", callback_data=f"page_{page+1}_{cat_name}|{state}"))
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("🌐 Job Portals", callback_data=f"portals_{cat_name}|{state}"),
        InlineKeyboardButton("🔄 New Search",  callback_data="back_states"),
    ])
    return InlineKeyboardMarkup(buttons)

def get_national_portals(cat_name, keyword, state):
    kw  = urllib.parse.quote(keyword)
    loc = urllib.parse.quote(state)
    cat_type = CAT_TYPE.get(cat_name, "IT")
    if cat_type == "IT":
        return [
            ("Indeed",        f"https://in.indeed.com/jobs?q={kw}&l={loc}"),
            ("Naukri",        f"https://www.naukri.com/{keyword.replace(' ','-')}-jobs-in-{state.replace(' ','-')}"),
            ("LinkedIn",      f"https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}"),
            ("Foundit",       f"https://www.foundit.in/srp/results?query={kw}&locations={loc}"),
            ("Internshala",   f"https://internshala.com/jobs/{kw}-jobs"),
            ("Freshersworld", f"https://www.freshersworld.com/jobs/jobsearch/{keyword.replace(' ','-')}-jobs-in-{state.replace(' ','-')}"),
            ("Apna",          f"https://apna.co/jobs?search={kw}&location={loc}"),
            ("WorkIndia",     f"https://www.workindia.in/jobs-{kw}-in-{loc}/"),
            ("NCS",           f"https://www.ncs.gov.in/job-seeker/Pages/job-search.aspx?Keyword={kw}&Location={loc}"),
        ]
    elif cat_type == "GOVT":
        return [("SarkariResult","https://www.sarkariresult.com"),("FreeJobAlert","https://www.freejobalert.com"),("IndGovtJobs","https://www.indgovtjobs.in"),("Employment News","https://www.employmentnews.gov.in"),("NCS Govt",f"https://www.ncs.gov.in/job-seeker/Pages/job-search.aspx?Keyword=government+jobs&Location={loc}"),("Rojgar Result","https://www.rojgarresult.com")]
    elif cat_type == "POLICE":
        return [("FreeJobAlert Police","https://www.freejobalert.com/police-jobs"),("SarkariResult Police","https://www.sarkariresult.com/police"),("GovtJobGuru Police","https://govtjobguru.in/police-jobs")]
    elif cat_type == "RAILWAY":
        return [("RRB Official","https://www.rrbcdg.gov.in"),("Indian Railways","https://www.indianrailways.gov.in"),("RailwayBharti","https://www.railwaybharti.in"),("FreeJobAlert Railway","https://www.freejobalert.com/railway-jobs")]
    elif cat_type == "BANK":
        return [("IBPS Official","https://www.ibps.in"),("SBI Careers","https://sbi.co.in/careers"),("BankersAdda","https://www.bankersadda.com/bank-jobs"),("FreeJobAlert Bank","https://www.freejobalert.com/bank-jobs")]
    elif cat_type == "COURT":
        return [("eCourts India","https://services.ecourts.gov.in"),("Supreme Court","https://www.sci.gov.in/recruitment"),("FreeJobAlert Court","https://www.freejobalert.com/court-jobs")]
    return []

def build_portals_msg(cat_name, keyword, state):
    national = get_national_portals(cat_name, keyword, state)
    text = "🌐 *NATIONAL JOB PORTALS*\n━━━━━━━━━━━━━━━━━━━━\n"
    for site, link in national:
        text += f"🔹 *{site}*\n💼 {keyword} | 📍 {state}\n🔗 [Search on {site}]({make_redirect_link(site, link)})\n\n"

    # State portals
    all_sites = state_sites_all.get(state, {})
    cat_type  = CAT_TYPE.get(cat_name, "IT")
    local     = all_sites.get(cat_type, []) or get_default_state_portals(cat_name, state)
    if local:
        text += "🏛 *STATE PORTALS*\n━━━━━━━━━━━━━━━━━━━━\n"
        for site, link in local:
            text += f"🔸 *{site}*\n📍 {state} | 💼 {keyword}\n🔗 [Open {site}]({make_redirect_link(site, link)})\n\n"
    return text

# ── KEYBOARDS ─────────────────────────────────────────────
def states_keyboard():
    buttons, row = [], []
    for state in states:
        row.append(InlineKeyboardButton(state, callback_data=f"state_{state}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def categories_keyboard(state):
    buttons, row = [], []
    for cat in categories:
        row.append(InlineKeyboardButton(cat, callback_data=f"cat_{state}|{cat}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅ Back to States", callback_data="back_states")])
    return InlineKeyboardMarkup(buttons)

# ── HANDLERS ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    await update.message.reply_text(
        "🇮🇳 *Welcome to Job Hire India Bot!*\n\n"
        "✅ Live job listings updated daily\n"
        "✅ All 28 Indian states\n"
        "✅ Government & private jobs\n"
        "✅ 19 job categories\n\n"
        "*Select your State:*",
        parse_mode="Markdown",
        reply_markup=states_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n\n"
        "1️⃣ /start — Select state\n"
        "2️⃣ Select job category\n"
        "3️⃣ Browse live jobs with Next/Back!\n\n"
        "/latest — 5 latest jobs\n"
        "/search python developer\n\n"
        f"🌐 {WEBSITE_BASE}",
        parse_mode="Markdown"
    )

async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = get_latest_jobs(5)
    if not jobs:
        await update.message.reply_text("No jobs yet. Check back soon! ⏳")
        return
    msg = "🆕 *Latest Jobs*\n━━━━━━━━━━━━━━\n\n"
    for j in jobs:
        msg += f"• [{j['title']}]({j['link']}) — {j['company']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(context.args).strip()
    if not keyword:
        await update.message.reply_text("Usage: /search python developer")
        return
    jobs = search_db(keyword)
    if not jobs:
        await update.message.reply_text(f"😔 No jobs found for *{keyword}*.", parse_mode="Markdown")
        return
    msg = f"🔍 *Results for '{keyword}':*\n\n"
    for j in jobs[:8]:
        msg += f"• [{j['title']}]({j['link']}) — {j['company']} | {j['location']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def testapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Testing JSearch API...")
    try:
        jobs = fetch_live_jobs("software developer", "Telangana")
        if jobs:
            msg = f"✅ JSearch API Working! Got {len(jobs)} jobs:\n\n"
            for j in jobs[:3]:
                msg += f"• {j.get('job_title')} — {j.get('employer_name','')}\n"
        else:
            msg = "❌ API returned 0 jobs"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ API Error: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id

    if data == "back_states":
        _job_cache.pop(user_id, None)
        await query.edit_message_text("🇮🇳 *Select Your State:*", parse_mode="Markdown", reply_markup=states_keyboard())

    elif data.startswith("state_"):
        state = data.replace("state_", "")
        await query.edit_message_text(
            f"📍 *{state}*\n\nChoose a Job Category:",
            parse_mode="Markdown",
            reply_markup=categories_keyboard(state)
        )

    elif data.startswith("cat_"):
        parts    = data.replace("cat_", "").split("|", 1)
        state    = parts[0]
        cat_name = parts[1] if len(parts) > 1 else ""
        keyword  = categories.get(cat_name, cat_name)

        await query.edit_message_text(
            f"⏳ Fetching *{cat_name}* jobs in *{state}*...\n\nPlease wait...",
            parse_mode="Markdown"
        )

        # Fetch all jobs
        jobs = fetch_live_jobs(keyword, state) if cat_name in LIVE_JOB_CATEGORIES else []

        # Cache jobs for pagination
        total_pages = max(1, (len(jobs) + JOBS_PER_PAGE - 1) // JOBS_PER_PAGE) if jobs else 1
        _job_cache[user_id] = {"jobs": jobs, "cat": cat_name, "state": state, "keyword": keyword, "total_pages": total_pages}

        if jobs:
            text = format_jobs_page(jobs, 0, total_pages, cat_name, state)
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=jobs_nav_keyboard(0, total_pages, cat_name, state)
            )
        else:
            # No live jobs — show portals directly
            portals_msg = build_portals_msg(cat_name, keyword, state)
            await query.edit_message_text(
                f"🔥 *{cat_name} Jobs in {state}*\n\n" + portals_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Change State", callback_data="back_states")]])
            )

    elif data.startswith("page_"):
        # Handle pagination
        parts    = data.replace("page_", "").split("_", 1)
        page     = int(parts[0])
        rest     = parts[1] if len(parts) > 1 else ""
        p2       = rest.split("|", 1)
        cat_name = p2[0]
        state    = p2[1] if len(p2) > 1 else ""

        cache = _job_cache.get(user_id)
        if not cache:
            await query.edit_message_text("⏳ Session expired! Please /start again.")
            return

        jobs        = cache["jobs"]
        total_pages = cache["total_pages"]
        text        = format_jobs_page(jobs, page, total_pages, cat_name, state)

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=jobs_nav_keyboard(page, total_pages, cat_name, state)
        )

    elif data.startswith("portals_"):
        rest     = data.replace("portals_", "")
        p        = rest.split("|", 1)
        cat_name = p[0]
        state    = p[1] if len(p) > 1 else ""
        keyword  = categories.get(cat_name, cat_name)
        portals_msg = build_portals_msg(cat_name, keyword, state)
        await query.edit_message_text(
            f"🔥 *{cat_name} Jobs in {state}*\n\n" + portals_msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Back to Jobs", callback_data=f"page_0_{cat_name}|{state}")],
                [InlineKeyboardButton("🔄 New Search",  callback_data="back_states")],
            ])
        )

# ── ADMIN COMMANDS ────────────────────────────────────────
def is_admin(update):
    return update.effective_user.id == ADMIN_ID

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    cache_info = get_cache_status()
    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Users: *{user_count()}*\n"
        f"💼 Jobs in DB: *{job_count()}*\n\n"
        f"🗄 *Cache Status:*\n{cache_info}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /broadcast your message here")
        return
    users = get_all_users()
    sent  = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"📣 Sent to {sent}/{len(users)} users.")

async def refresh_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to force refresh all cache"""
    if not is_admin(update): return
    await update.message.reply_text("🔄 Clearing cache and refreshing...")
    _cache_store.clear()
    await update.message.reply_text("✅ Cache cleared! Next search will fetch fresh jobs.")

async def addjob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    try:
        parts = [p.strip() for p in " ".join(context.args).split("|")]
        title, company, location, category, link = parts[:5]
        upsert_job(title, company, location, category, link, "manual")
        await update.message.reply_text(f"✅ Job added: *{title}*", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("Usage:\n`/addjob title|company|location|category|link`", parse_mode="Markdown")

# ── AUTO ALERTS ───────────────────────────────────────────
ALERT_KEYWORDS = [
    ("software developer","💻 Software Developer"),
    ("government jobs","🏛 Government Jobs"),
    ("bank jobs","🏦 Bank Jobs"),
    ("data scientist","🤖 AI / Data Jobs"),
    ("railway jobs","🚆 Railway Jobs"),
]

async def send_job_alerts(bot):
    users = get_all_users()
    if not users: return
    import random
    keyword, cat_name = random.choice(ALERT_KEYWORDS)
    jobs = fetch_live_jobs(keyword, "India")
    if not jobs: return
    msg  = f"🔔 *New Job Alerts — {cat_name}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, job in enumerate(jobs[:5], 1):
        title    = job.get("job_title", "Job Opening")
        company  = job.get("employer_name", "Company")
        location = f"{job.get('job_city','') or ''} {job.get('job_state','') or ''}".strip() or "India"
        raw_link = job.get("job_apply_link", "")
        link     = make_redirect_link(company, raw_link) if raw_link else ""
        msg += f"{i}. *{title}*\n🏢 {company}\n📍 {location}\n🔗 [Apply Now]({link})\n\n"
    msg += "🔍 Use /start to search more jobs!"
    sent = 0
    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown", disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Alert failed {uid}: {e}")
    logging.info(f"Alerts sent to {sent}/{len(users)} users")

async def alert_scheduler(bot):
    while True:
        try:
            await send_job_alerts(bot)
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
        await asyncio.sleep(6 * 60 * 60)

# ── MAIN ──────────────────────────────────────────────────
def main():
    init_db()
    print("✅ Job Hire India Bot is Starting...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("latest",    latest_command))
    app.add_handler(CommandHandler("search",    search_command))
    app.add_handler(CommandHandler("stats",     stats))
    app.add_handler(CommandHandler("addjob",    addjob))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("testapi",   testapi))
    app.add_handler(CommandHandler("refresh",   refresh_cache))
    app.add_handler(CallbackQueryHandler(button_handler))

    async def post_init(application):
        asyncio.create_task(alert_scheduler(application.bot))

    app.post_init = post_init
    print("✅ Job Hire India Bot is Running! 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
