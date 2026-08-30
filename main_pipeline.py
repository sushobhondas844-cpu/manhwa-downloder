import os
import re
import json
import time
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from PIL import Image

import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from playwright.sync_api import sync_playwright

# ==========================================
# CONFIGURATION
# ==========================================
SPREADSHEET_ID = "1Ww6sDCL8gMoJxdwOwz3kw0YIedQEdAkSNoRfj0U3wDk"  # Manhwa Tracker
RAW_STAGING_FOLDER_ID = "1EGeKmI9y_7iV3Wu9LuTEJLk_5vTgZXpk"       # Raw_Manhwa_Staging
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ==========================================
# 1. AUTHENTICATION ENGINE
# ==========================================
def get_google_services():
    print("Authenticating with Google Services...")
    sa_json_str = os.environ.get("NEXUS_DRIVE_CREDS")
    if not sa_json_str:
        raise ValueError("Missing 'NEXUS_DRIVE_CREDS' environment variable in GitHub Secrets.")
    
    sa_info = json.loads(sa_json_str)
    sheet_creds = SACredentials.from_service_account_info(sa_info, scopes=SHEET_SCOPES)
    gc = gspread.authorize(sheet_creds)
    print("Google Sheets authentication successful.")

    oauth_json_str = os.environ.get("DRIVE_OAUTH_TOKEN")
    if not oauth_json_str:
        raise ValueError("Missing 'DRIVE_OAUTH_TOKEN' environment variable in GitHub Secrets.")
        
    oauth_info = json.loads(oauth_json_str)
    drive_creds = OAuthCredentials(
        token=oauth_info.get("token"),
        refresh_token=oauth_info.get("refresh_token"),
        token_uri=oauth_info.get("token_uri"),
        client_id=oauth_info.get("client_id"),
        client_secret=oauth_info.get("client_secret")
    )
    drive_service = build("drive", "v3", credentials=drive_creds)
    print("Google Drive authentication successful.")
    return gc, drive_service

# ==========================================
# 2. RESILIENT NETWORK SESSION
# ==========================================
def create_resilient_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session

# ==========================================
# 3. PLAYWRIGHT HEADLESS BROWSER ENGINE
# ==========================================
def extract_html_with_playwright(chapter_url):
    print(f"Launching headless browser to load: {chapter_url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1080}
        )
        page = context.new_page()
        page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
        
        print("Scrolling down page to render all lazy-loaded images...")
        for _ in range(20):
            page.mouse.wheel(0, 1500)
            time.sleep(0.3)
            
        time.sleep(2.0)
        html_content = page.content()
        browser.close()
        return html_content

# ==========================================
# 4. PANEL EXTRACTION ENGINE
# ==========================================
def extract_panel_urls(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    panel_urls = []

    # Strategy 1: Check embedded script arrays (ts_reader, chapter_data)
    scripts = soup.find_all("script")
    for s in scripts:
        if s.string and any(key in s.string for key in ["ts_reader", "chapter_data", "sources", "images"]):
            urls = re.findall(r'https?:[^\"]+?\.(?:jpg|jpeg|png|webp)', s.string)
            for u in urls:
                clean_u = u.replace("\\/", "/")
                if clean_u not in panel_urls:
                    panel_urls.append(clean_u)
            if panel_urls:
                print(f"Extracted {len(panel_urls)} panels from embedded JavaScript array.")
                return panel_urls

    # Strategy 2: Extract in natural DOM order from reader container
    reader_container = (
        soup.select_one("#readerarea") or 
        soup.select_one(".reading-content") or 
        soup.select_one(".entry-content") or 
        soup.select_one("div[id*='image-container']") or 
        soup
    )

    lazy_attributes = ["data-src", "data-lazy-src", "data-original", "data-url", "src"]
    for img in reader_container.find_all("img"):
        img_url = None
        for attr in lazy_attributes:
            val = img.get(attr)
            if val and not val.startswith("data:image"):
                img_url = val.strip()
                break
                
        if img_url:
            full_url = urljoin(base_url, img_url)
            if any(ext in full_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                if not any(noise in full_url.lower() for noise in ['logo', 'icon', 'avatar', 'banner_small']):
                    if full_url not in panel_urls:
                        panel_urls.append(full_url)
                        
    print(f"Extracted {len(panel_urls)} panels from page DOM.")
    return panel_urls

# ==========================================
# 5. INTEGRITY VERIFICATION ENGINE
# ==========================================
def verify_and_save_image(image_bytes, target_path, min_size_kb=15):
    if len(image_bytes) < min_size_kb * 1024:
        return False
    try:
        with open(target_path, "wb") as f:
            f.write(image_bytes)
        with Image.open(target_path) as img:
            img.verify()
        return True
    except Exception:
        if os.path.exists(target_path):
            os.remove(target_path)
        return False

# ==========================================
# 6. DRIVE INGESTION ENGINE
# ==========================================
def upload_folder_to_drive(drive_service, local_folder, folder_name, parent_id):
    print(f"Creating Drive folder '{folder_name}' in Raw_Manhwa_Staging...")
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(body=file_metadata, fields='id').execute()
    created_id = folder.get('id')

    files = sorted(os.listdir(local_folder))
    print(f"Uploading {len(files)} panels to Google Drive...")
    for fname in files:
        fpath = os.path.join(local_folder, fname)
        if os.path.isfile(fpath):
            media = MediaFileUpload(fpath, mimetype='image/jpeg', resumable=True)
            f_metadata = {'name': fname, 'parents': [created_id]}
            drive_service.files().create(body=f_metadata, media_body=media, fields='id').execute()
    print("Drive upload complete.")
    return created_id

# ==========================================
# 7. MAIN QUEUE WORKFLOW
# ==========================================
def process_queue():
    gc, drive_service = get_google_services()
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    records = queue_ws.get_all_records()
    print(f"Found {len(records)} total entries in Download_Queue.")

    session = create_resilient_session()

    pending_found = False
    for idx, row in enumerate(records, start=2):
        status = str(row.get("Download Status", "")).strip().lower()
        if status != "pending":
            continue

        pending_found = True
        series_name = row.get("Series Title")
        chapter_num = row.get("Chapter Number")
        chapter_url = row.get("Direct Chapter Web URL")

        print(f"\n==================================================")
        print(f"Processing Task (Row {idx}): {series_name} - Chapter {chapter_num}")
        print(f"URL: {chapter_url}")
        print(f"==================================================")

        if not chapter_url or not str(chapter_url).startswith("http"):
            print(f"Row {idx} has an invalid URL. Skipping.")
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)

        try:
            page_html = extract_html_with_playwright(chapter_url)
            panel_urls = extract_panel_urls(page_html, chapter_url)
            
            if not panel_urls:
                print(f"No panels found for {series_name} Ch {chapter_num}. Skipping.")
                continue

            success_count = 0
            session.headers["Referer"] = chapter_url
            for p_idx, p_url in enumerate(panel_urls, start=1):
                target_path = os.path.join(local_dir, f"panel_{p_idx:03d}.jpg")
                try:
                    p_resp = session.get(p_url, timeout=25)
                    if p_resp.status_code == 200:
                        if verify_and_save_image(p_resp.content, target_path):
                            success_count += 1
                except Exception as e:
                    print(f"Panel {p_idx} download failed: {e}")
                time.sleep(0.1)

            print(f"Successfully verified {success_count}/{len(panel_urls)} panels locally.")

            if success_count > 0:
                gdrive_name = f"{series_name}_Chapter_{chapter_num}".replace(" ", "_")
                uploaded_id = upload_folder_to_drive(drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID)
                
                # Update status and folder ID in sheet
                queue_ws.update_cell(idx, 4, "Downloaded")
                queue_ws.update_cell(idx, 5, f"Drive Folder ID: {uploaded_id}")
                print(f"Row {idx} updated to 'Downloaded' in Google Sheets.")
        except Exception as e:
            print(f"Error processing row {idx}: {e}")

    if not pending_found:
        print("No tasks with status 'Pending' found in queue.")

if __name__ == "__main__":
    process_queue()
