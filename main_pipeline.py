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
# Description: Authenticates Google Drive and Sheets using dual credentials.
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
# Description: Creates a HTTP session with automatic retries.
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
        "User\x2dAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept\x2dLanguage": "en\x2dUS,en;q=0.9",
    })
    return session

# Module 3: Network Interception Engine
# Description: Captures image URLs via browser rendering.
def extract_panels_with_playwright(chapter_url):
    captured_urls = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            viewport={"width": 1280, "height": 1080}
        )
        page = context.new_page()

        def handle_response(response):
            url = response.url
            if response.status == 200:
                is_image = "image" in response.headers.get("content\x2dtype", "") or any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp'])
                if is_image and ("asura" in url or "chapter" in url or "comics" in url):
                    if not any(noise in url.lower() for noise in ['logo', 'icon', 'avatar', 'badge', 'banner', 'ads']):
                        if url not in seen:
                            seen.add(url)
                            captured_urls.append(url)

        page.on("response", handle_response)
        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            browser.close()
            return []

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

# Module 4: Sequence Extraction Engine
# Description: Parses the digits located exactly before the file extension.
def get_sequential_number(url):
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    match = re.search(r'(\d+)\.(?:jpg|jpeg|png|webp)$', filename, re.IGNORECASE)
    if match:
        seq = int(match.group(1))
        return seq, filename
    return -1, filename

# Module 5: Continuity Filter Engine
# Description: Builds an unbroken numerical chain and completely blocks massive erratic jumps.
def filter_chronological_chain(sequenced_panels, max_gap=2):
    if not sequenced_panels:
        return []
    
    sequenced_panels.sort(key=lambda x: x[0])
    valid_chain = [sequenced_panels[0]]
    current_seq = sequenced_panels[0][0]
    
    for panel in sequenced_panels[1:]:
        seq_num = panel[0]
        jump = seq_num - current_seq
        
        if 0 <= jump <= max_gap:
            valid_chain.append(panel)
            current_seq = seq_num
        elif jump > max_gap:
            break
            
    return valid_chain

# Module 6: Verification Engine
# Description: Validates bytes to discard corrupted files.
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

# Module 7: Drive Ingestion Engine
# Description: Uploads chronologically verified files to Drive.
def upload_folder_to_drive(drive_service, local_folder, folder_name, parent_id):
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google\x2dapps.folder',
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

# Module 8: Main Workflow Engine
# Description: Coordinates extraction queue and sequential downloads.
def process_queue():
    gc, drive_service = get_google_services()
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    records = queue_ws.get_all_records()
    session = create_resilient_session()

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Download Status", "")).strip()
        if status != "Pending" and status != "":
            continue
        
        series_name = row.get("Series Title")
        chapter_num = row.get("Chapter Number")
        chapter_url = row.get("Direct Chapter Web URL")
        
        error_msg = "Link doesn't work, find a new better link"
        
        if not chapter_url or not str(chapter_url).startswith("http"):
            queue_ws.update_cell(idx, 4, error_msg)
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)
        
        panel_urls = extract_panels_with_playwright(chapter_url)
        
        if not panel_urls:
            queue_ws.update_cell(idx, 4, error_msg)
            continue
        
        raw_sequences = []
        for url in panel_urls:
            seq, fname = get_sequential_number(url)
            if seq >= 0:
                raw_sequences.append((seq, fname, url))
                
        valid_panels = filter_chronological_chain(raw_sequences)
        
        success_count = 0
        for seq, original_fname, p_url in valid_panels:
            target_path = os.path.join(local_dir, original_fname)
            try:
                session.headers["Referer"] = chapter_url
                p_resp = session.get(p_url, timeout=25)
                if p_resp.status_code == 200:
                    if verify_and_save_image(p_resp.content, target_path):
                        success_count += 1
            except Exception:
                pass
            time.sleep(0.05)

        if success_count > 0:
            gdrive_name = f"{series_name}_Chapter_{chapter_num}"
            uploaded_id = upload_folder_to_drive(drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID)
            queue_ws.update_cell(idx, 4, "Downloaded")
            queue_ws.update_cell(idx, 5, f"Drive Folder ID: {uploaded_id}")
        else:
            queue_ws.update_cell(idx, 4, error_msg)

if __name__ == "__main__":
    process_queue()
