import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SPREADSHEET_ID = "1Ww6sDCL8gMoJxdwOwz3kw0YIedQEdAkSNoRfj0U3wDk"
RAW_STAGING_FOLDER_ID = "1EGeKmI9y_7iV3Wu9LuTEJLk_5vTgZXpk"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Module 1: Authentication Engine
# Function: Initializes Google Workspace connections using dual authentication methods to bypass drive quotas.
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
# Function: Creates a robust HTTP session with retries and headers to bypass simple bot blocks.
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
        "User\x2dAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,*/*;q=0.8",
        "Accept\x2dLanguage": "en\x2dUS,en;q=0.9",
    })
    return session

# Module 3: Extraction Engine
# Function: Parses HTML to locate high resolution image URLs from scripts and DOM elements.
def extract_panel_urls(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    panel_urls = []
    for s in soup.find_all("script"):
        if s.string and any(key in s.string for key in ["ts_reader", "chapter_data", "images"]):
            urls = re.findall(r'https?:[^"]+?\.(?:jpg|jpeg|png|webp)', s.string)
            for u in urls:
                clean_u = u.replace("\\/", "/")
                if clean_u not in panel_urls:
                    panel_urls.append(clean_u)
            if panel_urls:
                return panel_urls

    reader = soup.select_one("#readerarea") or soup
    lazy_attributes = ["data\x2dsrc", "data\x2dlazy\x2dsrc", "data\x2doriginal", "src"]
    for img in reader.find_all("img"):
        for attr in lazy_attributes:
            val = img.get(attr)
            if val and not val.startswith("data:image"):
                full_url = urljoin(base_url, val.strip())
                if any(ext in full_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    if full_url not in panel_urls:
                        panel_urls.append(full_url)
                break
    return panel_urls

# Module 4: Verification Engine
# Function: Validates downloaded bytes using Pillow to discard corrupt or empty images.
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

# Module 5: Drive Ingestion Engine
# Function: Uploads verified local files into the specified Google Drive folder.
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

# Module 6: Main Workflow Engine
# Function: Orchestrates queue processing, downloading, and uploading.
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
        
        session.headers["Referer"] = chapter_url
        resp = session.get(chapter_url, timeout=25)
        if resp.status_code != 200:
            continue

        panel_urls = extract_panel_urls(resp.text, chapter_url)
        success_count = 0
        
        for p_idx, p_url in enumerate(panel_urls, start=1):
            target_path = os.path.join(local_dir, f"panel_{p_idx:03d}.jpg")
            try:
                p_resp = session.get(p_url, timeout=25)
                if p_resp.status_code == 200:
                    if verify_and_save_image(p_resp.content, target_path):
                        success_count += 1
            except Exception:
                pass
            time.sleep(0.1)

        if success_count > 0:
            gdrive_name = f"{series_name}_Chapter_{chapter_num}"
            uploaded_id = upload_folder_to_drive(drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID)
            queue_ws.update_cell(idx, 4, "Downloaded")
            queue_ws.update_cell(idx, 5, f"Drive Folder ID: {uploaded_id}")

if __name__ == "__main__":
    process_queue()
