import os
import re
import json
import time
import requests
from urllib.parse import urlparse
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from playwright.sync_api import sync_playwright

SPREADSHEET_ID = "1Ww6sDCL8gMoJxdwOwz3kw0YIedQEdAkSNoRfj0U3wDk"
RAW_STAGING_FOLDER_ID = "1EGeKmI9y_7iV3Wu9LuTEJLk_5vTgZXpk"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Module 1: Authentication Engine
def get_google_services():
    sa_json_str = os.environ.get("NEXUS_DRIVE_CREDS")
    sa_info = json.loads(sa_json_str)
    sheet_creds = SACredentials.from_service_account_info(sa_info, scopes=SHEET_SCOPES)
    gc = gspread.authorize(sheet_creds)
    
    oauth_json_str = os.environ.get("DRIVE_OAUTH_TOKEN")
    oauth_info = json.loads(oauth_json_str)
    drive_creds = OAuthCredentials(
        token=oauth_info.get("token"),
        refresh_token=oauth_info.get("refresh_token"),
        token_uri=oauth_info.get("token_uri"),
        client_id=oauth_info.get("client_id"),
        client_secret=oauth_info.get("client_secret")
    )
    drive_service = build("drive", "v3", credentials=drive_creds)
    return gc, drive_service

# Module 2: Network Session Engine
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session

# Module 3: Network Interception & Full-Scroll Playwright Engine
def extract_panels_with_playwright(chapter_url):
    captured_urls = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1080}
        )
        page = context.new_page()

        # Intercept CDN responses directly
        def handle_response(response):
            url = response.url
            if response.status == 200:
                is_image = "image" in response.headers.get("content-type", "") or any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp'])
                if is_image and ("asura-images/chapters" in url or "chapter" in url or "comics" in url):
                    # Filter UI icons, avatars, and ads
                    if not any(noise in url.lower() for noise in ['logo', 'icon', 'avatar', 'badge', 'banner', 'ads', 'discord']):
                        if url not in seen:
                            seen.add(url)
                            captured_urls.append(url)

        page.on("response", handle_response)
        
        print(f"Loading page in headless browser: {chapter_url}")
        page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)

        # Dynamic scroll loop until the bottom of the page is reached
        last_height = page.evaluate("() => document.body.scrollHeight")
        no_change_count = 0

        while no_change_count < 5:
            page.evaluate("() => window.scrollBy(0, 1500)")
            time.sleep(0.4)
            new_height = page.evaluate("() => document.body.scrollHeight")
            
            if new_height == last_height:
                no_change_count += 1
            else:
                no_change_count = 0
                last_height = new_height

        page.wait_for_timeout(2500)
        browser.close()

    return captured_urls

# Module 4: Verification Engine
def verify_and_save_image(image_bytes, target_path, min_size_kb=10):
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

# Module 5: Drive Ingestion Engine
def upload_folder_to_drive(drive_service, local_folder, folder_name, parent_id):
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(body=file_metadata, fields='id').execute()
    created_id = folder.get('id')

    for fname in sorted(os.listdir(local_folder)):
        fpath = os.path.join(local_folder, fname)
        if os.path.isfile(fpath):
            media = MediaFileUpload(fpath, mimetype='image/jpeg', resumable=True)
            f_metadata = {'name': fname, 'parents': [created_id]}
            drive_service.files().create(body=f_metadata, media_body=media, fields='id').execute()
    return created_id

# Module 6: Main Workflow Engine
def process_queue():
    gc, drive_service = get_google_services()
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    records = queue_ws.get_all_records()
    session = create_resilient_session()

    for idx, row in enumerate(records, start=2):
        if row.get("Download Status") != "Pending":
            continue
        
        series_name = row.get("Series Title")
        chapter_num = row.get("Chapter Number")
        chapter_url = row.get("Direct Chapter Web URL")
        
        if not chapter_url or not str(chapter_url).startswith("http"):
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)
        
        print("==================================================")
        print(f"Processing Task (Row {idx}): {series_name} - Chapter {chapter_num}")
        print("==================================================")

        panel_urls = extract_panels_with_playwright(chapter_url)
        print(f"Captured {len(panel_urls)} panels via network interception.")
        
        success_count = 0
        for p_idx, p_url in enumerate(panel_urls, start=1):
            target_path = os.path.join(local_dir, f"panel_{p_idx:03d}.jpg")
            try:
                session.headers["Referer"] = chapter_url
                p_resp = session.get(p_url, timeout=25)
                if p_resp.status_code == 200:
                    if verify_and_save_image(p_resp.content, target_path):
                        success_count += 1
            except Exception:
                pass
            time.sleep(0.05)

        print(f"Successfully verified {success_count}/{len(panel_urls)} panels locally.")

        if success_count > 0:
            gdrive_name = f"{series_name}_Chapter_{chapter_num}"
            uploaded_id = upload_folder_to_drive(drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID)
            queue_ws.update_cell(idx, 4, "Downloaded")
            queue_ws.update_cell(idx, 5, f"Drive Folder ID: {uploaded_id}")
            print(f"Drive upload complete. Row {idx} updated to 'Downloaded' in Google Sheets.")

if __name__ == "__main__":
    process_queue()
