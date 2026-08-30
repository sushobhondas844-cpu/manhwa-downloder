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
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==========================================================
# GOOGLE WORKSPACE CONFIGURATION (GROUNDED IDs)
# ==========================================================
SPREADSHEET_ID = "1Ww6sDCL8gMoJxdwOwz3kw0YIedQEdAkSNoRfj0U3wDk"  # Manhwa Tracker ID
RAW_STAGING_FOLDER_ID = "1EGeKmI9y_7iV3Wu9LuTEJLk_5vTgZXpk"       # Raw_Manhwa_Staging Folder ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_google_services():
    """Authenticates using the Service Account JSON stored in GitHub Secrets."""
    sa_json_str = os.environ.get("GCP_SA_KEY")
    if not sa_json_str:
        raise ValueError("Missing 'GCP_SA_KEY' environment variable. Set it in GitHub Secrets.")
    
    sa_info = json.loads(sa_json_str)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

def create_resilient_session():
    """Creates a requests session with retries and browser emulation headers."""
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

def extract_panel_urls(page_html, base_url):
    """
    Extracts high-resolution panel URLs using:
    1. Embedded JavaScript JSON arrays (used on WordPress/Madara reader themes).
    2. Reader-container scoped DOM extraction with lazy-load tag fallback.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    panel_urls = []

    # Strategy 1: Check for script JSON blocks (ts_reader, chapter_data, images)
    scripts = soup.find_all("script")
    for s in scripts:
        if s.string and any(key in s.string for key in ["ts_reader", "chapter_data", "sources", "images"]):
            urls = re.findall(r'https?:[^\"]+?\.(?:jpg|jpeg|png|webp)', s.string)
            for u in urls:
                clean_u = u.replace("\\/", "/")
                if clean_u not in panel_urls:
                    panel_urls.append(clean_u)
            if panel_urls:
                return panel_urls

    # Strategy 2: Scope extraction to reader-specific containers
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
                if not any(noise in full_url.lower() for noise in ['logo', 'avatar', 'icon', 'banner_small']):
                    if full_url not in panel_urls:
                        panel_urls.append(full_url)
                        
    return panel_urls

def verify_and_save_image(image_bytes, target_path, min_size_kb=15):
    """Verifies image integrity using Pillow to discard corrupt or placeholder files."""
    if len(image_bytes) < min_size_kb * 1024:
        return False, "File too small (likely a placeholder)"
        
    try:
        with open(target_path, "wb") as f:
            f.write(image_bytes)
        with Image.open(target_path) as img:
            img.verify()
        return True, "Valid"
    except Exception as e:
        if os.path.exists(target_path):
            os.remove(target_path)
        return False, f"Corrupt image: {e}"

def upload_folder_to_drive(drive_service, local_folder, folder_name, parent_id):
    """Creates a dedicated chapter folder in Google Drive and uploads all verified panels."""
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = drive_service.files().create(body=file_metadata, fields='id').execute()
    created_folder_id = folder.get('id')

    for fname in sorted(os.listdir(local_folder)):
        fpath = os.path.join(local_folder, fname)
        if os.path.isfile(fpath):
            media = MediaFileUpload(fpath, mimetype='image/jpeg', resumable=True)
            f_metadata = {'name': fname, 'parents': [created_folder_id]}
            drive_service.files().create(body=f_metadata, media_body=media, fields='id').execute()

    return created_folder_id

def process_queue():
    """Main workflow: checks sheet queue, downloads panels, and uploads to Drive."""
    print("Connecting to Google Sheets and Drive...")
    gc, drive_service = get_google_services()
    
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    
    records = queue_ws.get_all_records()
    print(f"Total entries in queue: {len(records)}")

    session = create_resilient_session()

    for idx, row in enumerate(records, start=2): # Row 1 is the header
        status = row.get("Download Status")
        if status != "Pending":
            continue

        series_name = row.get("Series Title")
        chapter_num = row.get("Chapter Number")
        chapter_url = row.get("Direct Chapter Web URL")

        if not chapter_url or not str(chapter_url).startswith("http"):
            print(f"Skipping Row {idx}: Invalid URL")
            continue

        print(f"\n==========================================")
        print(f"Processing: {series_name} - Chapter {chapter_num}")
        print(f"URL: {chapter_url}")
        print(f"==========================================")

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)

        session.headers["Referer"] = chapter_url
        resp = session.get(chapter_url, timeout=25)
        if resp.status_code != 200:
            print(f"Failed to fetch page (HTTP {resp.status_code})")
            continue

        panel_urls = extract_panel_urls(resp.text, chapter_url)
        print(f"Found {len(panel_urls)} candidate panel URLs.")
        if not panel_urls:
            print("No panel URLs detected. Skipping.")
            continue

        success_count = 0
        for p_idx, p_url in enumerate(panel_urls, start=1):
            target_path = os.path.join(local_dir, f"panel_{p_idx:03d}.jpg")
            try:
                p_resp = session.get(p_url, timeout=25)
                if p_resp.status_code == 200:
                    is_valid, _ = verify_and_save_image(p_resp.content, target_path)
                    if is_valid:
                        success_count += 1
            except Exception as e:
                print(f"Error downloading panel {p_idx}: {e}")
            time.sleep(0.1)

        print(f"Verified {success_count}/{len(panel_urls)} panels locally.")

        if success_count > 0:
            # Upload to Google Drive Raw_Manhwa_Staging folder
            gdrive_folder_name = f"{series_name}_Chapter_{chapter_num}"
            print(f"Uploading '{gdrive_folder_name}' to Google Drive...")
            uploaded_folder_id = upload_folder_to_drive(
                drive_service, local_dir, gdrive_folder_name, RAW_STAGING_FOLDER_ID
            )
            
            # Update status in Google Sheets (Column D = Download Status, Column E = Staging Path)
            queue_ws.update_cell(idx, 4, "Downloaded")
            queue_ws.update_cell(idx, 5, f"Drive Folder ID: {uploaded_folder_id}")
            print(f"Row {idx} updated to 'Downloaded' in Manhwa Tracker.")

if __name__ == "__main__":
    process_queue()
