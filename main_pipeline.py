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

# OG
import os
import re
import time
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Module 1: Headless Browser Engine
# Function: Boots a chromium browser, loads the page, and scrolls smoothly to trigger all JavaScript lazy loaded images.
def extract_html_with_playwright(chapter_url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(chapter_url, timeout=60000)
        
        for i in range(15):
            page.mouse.wheel(0, 2000)
            time.sleep(0.5)
            
        html_content = page.content()
        browser.close()
        return html_content

# Module 2: Native Number Extractor
# Function: Isolates the filename from the URL and extracts the final integer sequence for chronological sorting.
def get_sequential_number(url):
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    numbers = re.findall(r'\d+', filename)
    if numbers:
        return int(numbers[-1])
    return -1

# Module 3: Sequential Sorting Engine
# Function: Filters out UI elements and sorts the genuine chapter panels based strictly on their native URL numbers.
def extract_and_sort_panel_urls(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    raw_urls = set()
    
    for img in soup.find_all("img"):
        for attr in ["src", "data_src", "data_lazy_src"]:
            val = img.get(attr)
            if val and val.startswith("http"):
                if any(ext in val.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    raw_urls.add(val)
    
    sequenced_panels = []
    for raw_url in raw_urls:
        seq_num = get_sequential_number(raw_url)
        if seq_num >= 0 and not any(noise in raw_url.lower() for noise in ['logo', 'icon', 'avatar']):
            sequenced_panels.append((seq_num, raw_url))
            
    sequenced_panels.sort(key=lambda x: x[0])
    return [panel[1] for panel in sequenced_panels]

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
