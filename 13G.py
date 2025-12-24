import os
import requests
from datetime import datetime, timedelta, timezone

# ============ CONFIG ============

# 13D/13G filers we care about
FUNDS = [
    {"name": "Point72 Asset Management", "cik": "1603466"},
    {"name": "Elliott",        "cik": "1791786"},
    {"name": "Starboard Value",           "cik": "1517137"},
    {"name": "Jane Street",           "cik": "1595888"},
    {"name": "Renaissance",           "cik": "1037389"},
    {"name": "Citadel",           "cik": "1423053"},
    {"name": "Millennium",           "cik": "1273087"},
]

# sec-api.io endpoint (Form 13D/13G search)
SEC_API_URL = "https://api.sec-api.io/form-13d-13g"

# Put your SEC-API key in an env var, or hardcode if you must
SEC_API_KEY = os.getenv("SEC_API_KEY", "76d9e4b3bcba4e015a915503532bd98a9dd39706da64c424d067be43d472647f")

# Telegram config – strongly recommended to use env vars
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8547260576:AAG5GHEWGSoP3M7J2_69kmrpd0OLQhm6dhI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5937443021")

# How many days back to consider (just in case),
# but we only show the latest N filings anyway
LOOKBACK_DAYS = 365

# Show at most this many filings per fund
MAX_FILINGS_PER_FUND = 5

# Telegram max message length
MAX_TELEGRAM_LENGTH = 4000  # a bit lower than 4096 for safety


# ============ TELEGRAM HELPERS ============

def chunk_text(text: str, max_len: int = MAX_TELEGRAM_LENGTH):
    """
    Split text into chunks that are each <= max_len, preferably on newlines.
    """
    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at the last newline before max_len
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len

        chunk = remaining[:split_at]
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


def send_telegram_message(text: str) -> bool:
    """
    Send a (possibly long) message to Telegram, splitting into multiple
    messages if needed.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram token/chat_id not set. Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = chunk_text(text)
    all_ok = True

    for idx, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            header = f"(Part {idx}/{len(chunks)})\n"
            body = header + chunk
        else:
            body = chunk

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": body,
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                print(f"Telegram HTTP error on part {idx}: {resp.status_code}")
                print("Telegram response body:", resp.text)
                all_ok = False
            else:
                data = resp.json()
                if not data.get("ok", False):
                    print(f"Telegram API returned ok=False on part {idx}:", data)
                    all_ok = False
        except Exception as e:
            print(f"Error sending Telegram message part {idx}:", repr(e))
            all_ok = False

    if all_ok:
        print("Telegram daily report sent successfully.")
    else:
        print("Failed to send one or more parts of the Telegram daily report.")

    return all_ok


# ============ SEC-API HELPERS ============

def fetch_13dg_filings_for_cik(cik: str,
                               lookback_days: int = LOOKBACK_DAYS,
                               max_filings: int = MAX_FILINGS_PER_FUND):
    """
    Fetch recent 13D/13G filings for a given CIK via sec-api.io.

    Returns a list (max `max_filings`) of dicts:
      {
        "form": str,
        "filed_at_date": "YYYY-MM-DD",
        "issuer": str,
        "percent_owned": float or None
      }
    """
    if not SEC_API_KEY or SEC_API_KEY == "YOUR_SEC_API_KEY_HERE":
        raise RuntimeError("SEC_API_KEY is not set. Please set it as env var or in the script.")

    now_utc = datetime.now(timezone.utc)
    cutoff_date = now_utc.date() - timedelta(days=lookback_days)
    print(f"Fetching 13D/13G filings for CIK {cik} (cutoff: {cutoff_date})...")

    headers = {
        "Authorization": SEC_API_KEY,
        "Content-Type": "application/json",
    }

    # Lucene query: all filings for this filer’s CIK
    query = f"filers.cik:{cik} AND accessionNo:*"

    payload = {
        "query": query,
        "from": 0,
        "size": 50,  # get up to 50 most recent, then we'll clip to max_filings
        "sort": [{"filedAt": {"order": "desc"}}],
    }

    resp = requests.post(SEC_API_URL, headers=headers, json=payload, timeout=30)
    print("HTTP status (sec-api.io):", resp.status_code)
    resp.raise_for_status()

    data = resp.json()
    filings = data.get("filings", [])
    print(f"Total 13D/13G filings returned via sec-api.io for {cik}: {len(filings)}")

    results = []
    for f in filings:
        if len(results) >= max_filings:
            break  # already have enough recent ones

        form = f.get("formType", "")
        filed_at_raw = f.get("filedAt")  # e.g. "2025-11-24T17:12:34-04:00"
        issuer = f.get("nameOfIssuer", "")
        owners = f.get("owners", []) or []

        if not filed_at_raw:
            continue

        filed_date_str = filed_at_raw.split("T", 1)[0]
        try:
            filed_date = datetime.strptime(filed_date_str, "%Y-%m-%d").date()
        except Exception as e:
            print("Error parsing filedAt date:", filed_at_raw, "err:", e)
            continue

        if filed_date < cutoff_date:
            # older than lookback, skip
            continue

        # Extract percent owned. Different filings can have multiple owners with their own %.
        # Here we take the MAX amountAsPercent among owners as the headline %.
        percents = []
        for o in owners:
            p = o.get("amountAsPercent")
            if isinstance(p, (int, float)):
                percents.append(float(p))

        percent_owned = max(percents) if percents else None

        results.append({
            "form": form,
            "filed_at_date": filed_date_str,
            "issuer": issuer,
            "percent_owned": percent_owned,
        })

    print(f"Recent 13D/13G filings for {cik} (limited to {max_filings}): {len(results)}")
    return results


# ============ REPORT FORMATTING ============

def format_daily_report(all_funds_filings: dict) -> str:
    """
    all_funds_filings: dict[cik] -> {"name": str, "filings": [ ... ]}

    Returns a single text string for Telegram:
      For each fund, up to 5 lines:
        YYYY-MM-DD | Issuer | X.X%
    """
    now_utc = datetime.now(timezone.utc)
    lines = [
        "Daily 13D/13G Ownership Snapshot",
        f"Date (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Showing up to {MAX_FILINGS_PER_FUND} latest filings per fund",
        "",
    ]

    for cik, info in all_funds_filings.items():
        fund_name = info["name"]
        filings = info["filings"]

        lines.append("=" * 70)
        lines.append(f"{fund_name} (CIK {cik})")
        lines.append("-" * 70)
        lines.append("Date       | Issuer                               | % owned")
        lines.append("-" * 70)

        if not filings:
            lines.append("No 13D/13G filings in the lookback window.")
            lines.append("")
            continue

        for f in filings:
            date_str = f["filed_at_date"]
            issuer = (f["issuer"] or "").strip()
            if len(issuer) > 35:
                issuer = issuer[:32] + "..."

            if f["percent_owned"] is None:
                pct_str = "N/A"
            else:
                pct_str = f"{f['percent_owned']:.1f}%"

            line = f"{date_str} | {issuer:<35} | {pct_str}"
            lines.append(line)

        lines.append("")  # blank line between funds

    return "\n".join(lines)


# ============ MAIN ============

def main():
    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc.isoformat()}] Running daily 13D/13G report...")

    all_funds_filings = {}

    for fund in FUNDS:
        name = fund["name"]
        cik = fund["cik"]
        try:
            filings = fetch_13dg_filings_for_cik(cik)
        except Exception as e:
            print(f"Error fetching filings for {name} (CIK {cik}):", repr(e))
            filings = []

        all_funds_filings[cik] = {
            "name": name,
            "filings": filings,
        }

    report_text = format_daily_report(all_funds_filings)
    print("=== Report preview ===")
    print(report_text)
    print("=== End preview ===\n")

    ok = send_telegram_message(report_text)
    if not ok:
        print("Failed to send Telegram daily report.")
    else:
        print("Daily report sent.")


if __name__ == "__main__":
    main()
