# SupplyNote Report Automation

A production-ready Streamlit app that automates SupplyNote Food Cost Report generation, downloading, and emailing — per location, per date range.

---

## 📁 Project Structure

```
project/
├── app.py                    ← Streamlit UI & pipeline orchestrator
├── requirements.txt
├── .env.example              ← Copy to .env and fill in credentials
├── create_sample_excel.py    ← Run once to create sample Excel
│
├── config/
│   └── email_config.json     ← Email defaults (recipients, subject, body)
│
├── utils/
│   ├── excel_reader.py       ← Reads Excel (read-only, never writes)
│   └── email_sender.py       ← Sends reports via SMTP
│
├── automation/
│   └── playwright_script.py  ← Playwright automation (login → filter → download)
│
├── data/
│   └── report_filters.xlsx   ← Your location/city/state lookup table
│
└── reports/                  ← Downloaded reports saved here (auto-created)
```

---

## ⚡ Quick Start

### 1. Clone & install dependencies

```bash
cd project
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
SUPPLYNOTE_USER=your_email@example.com
SUPPLYNOTE_PASS=your_password

EMAIL_USER=your_gmail@gmail.com
EMAIL_PASS=your_gmail_app_password
```

> **Gmail App Password**: Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and generate one. Do NOT use your regular Gmail password.

### 3. Create the Excel data file

Either run the sample creator:

```bash
python create_sample_excel.py
```

Or create `data/report_filters.xlsx` manually with these exact columns:

| location | city | state |
|----------|------|-------|
| Outlet Mumbai Central | Mumbai | Maharashtra |
| Outlet Koramangala | Bengaluru | Karnataka |

### 4. Configure email defaults

Edit `config/email_config.json`:

```json
{
  "default_recipients": ["manager@yourcompany.com"],
  "cc": ["cfo@yourcompany.com"],
  "subject": "Daily Food Cost Report",
  "body": "Please find the attached reports."
}
```

### 5. Run the app

```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

---

## 🖥️ Using the UI

1. **Select Locations** — Multi-select from your Excel data
2. **Set Start Date & Time** — Supports 12-hour AM/PM via Streamlit time picker
3. **Set End Date & Time** — Same
4. **Override Email Recipients** *(optional)* — Comma-separated, overrides JSON config
5. Click **⚡ Generate Reports**

The UI shows a live progress row per location with status badges (Running / Done / Failed).

---

## 🤖 Playwright Selectors

The selectors in `automation/playwright_script.py` are **placeholders** designed to match common patterns. You will likely need to update them for SupplyNote's actual DOM.

### How to inspect selectors

1. Run with `headless=False` (change in `playwright_script.py`):
   ```python
   browser = await pw.chromium.launch(headless=False)
   ```
2. Open DevTools (`F12`) on SupplyNote
3. Right-click the element → Inspect → Copy selector

### Key selector locations in the code

| Function | What to update |
|----------|----------------|
| `_login` | Email, password, submit button selectors |
| `_navigate_to_report` | `REPORT_URL` constant + filter section selector |
| `_select_location` | Location dropdown selector |
| `_select_state` | State dropdown selector |
| `_select_city` | City dropdown selector |
| `_set_date_range` | Date & time input selectors |
| `_select_report_type` | Report type checkbox/radio/dropdown |
| `_generate_and_download` | Generate button selector |

---

## 📧 Email Setup (Gmail)

1. Enable 2-Factor Authentication on your Gmail account
2. Go to: **Google Account → Security → App Passwords**
3. Create an app password for "Mail"
4. Use that 16-character password as `EMAIL_PASS` in `.env`

---

## 🔍 Logs

- Console: real-time output
- `automation.log`: full log file (created in project root)
- `reports/ERROR_*.png`: screenshot saved on any automation failure

---

## ⚙️ Debugging Tips

| Problem | Solution |
|---------|----------|
| Login fails | Check credentials in `.env`; try `headless=False` |
| Selectors don't match | Inspect SupplyNote DOM and update selectors |
| Download doesn't trigger | Update `_generate_and_download` button selector |
| Email fails | Verify Gmail App Password, not regular password |
| Excel not loading | Ensure columns are exactly: `location`, `city`, `state` |
