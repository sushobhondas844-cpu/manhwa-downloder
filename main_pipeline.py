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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session

# Module 3: Network Interception Engine (DOM Ordered & Container Restricted)
def extract_panels_with_playwright(chapter_url):
    captured_urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            viewport={"width": 1280, "height": 1080}
        )
        page = context.new_page()

        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            browser.close()
            return []

        # Scroll to force lazy-loading
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

        # TARGET THE READER AREA FIRST to exclude ads and sidebars
        reader_selectors = ["#readerarea", ".reading-content", ".page-break", ".entry-content"]
        target_container = None
        
        for selector in reader_selectors:
            if page.query_selector(selector):
                target_container = page.query_selector(selector)
                break
        
        # Fallback to whole page if standard containers aren't found
        image_elements = target_container.query_selector_all("img") if target_container else page.query_selector_all("img")
        
        for img in image_elements:
            url = img.get_attribute("src") or img.get_attribute("data-src")
            if url and url.startswith("http"):
                is_valid_ext = any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp'])
                is_manhwa_host = any(keyword in url.lower() for keyword in ["asura", "chapter", "comics", "wp-manga"])
                is_noise = any(noise in url.lower() for noise in ['logo', 'icon', 'avatar', 'badge', 'banner', 'ads', 'discord'])
                
                if (is_valid_ext or is_manhwa_host) and not is_noise:
                    if url not in captured_urls:
                        captured_urls.append(url)

        browser.close()
    return captured_urls

# Module 4: Verification Engine
def verify_and_save_image(image_bytes, target_path, min_size_kb=20):
    # Lowered size floor to 20kb to prevent deleting small story panels
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

# Module 6: Relocation, Renaming & Purge Engine (Step 4)
def execute_relocation(gc, drive_service):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID)
        queue_ws = sheet.worksheet("Download_Queue")
        records = queue_ws.get_all_records()

        for idx, row in enumerate(records, start=2):
            status_key = next((k for k in row.keys() if "Download Status" in k), "Download Status")
            status = str(row.get(status_key, "")).strip().upper()

            if status == "READY TO MOVE":
                staging_id = str(row.get("Raw Staging Path", "")).strip()
                target_id = str(row.get("Processed Dataset Path", "")).strip()
                junk_notes = str(row.get("Link Notes", "")).strip()
                rename_map_raw = str(row.get("Panel Sequence & Rename Map", "")).strip()

                if not staging_id or not target_id:
                    continue

                try:
                    rename_dict = {}
                    if rename_map_raw:
                        pairs = re.split(r'[,;\n]', rename_map_raw)
                        for pair in pairs:
                            if ':' in pair:
                                k, v = pair.split(':', 1)
                                rename_dict[k.strip()] = v.strip()
                            elif '->' in pair:
                                k, v = pair.split('->', 1)
                                rename_dict[k.strip()] = v.strip()
                            elif '=' in pair:
                                k, v = pair.split('=', 1)
                                rename_dict[k.strip()] = v.strip()

                    query = f"'{staging_id}' in parents and trashed = false"
                    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
                    files = results.get('files', [])

                    junk_keywords = [j.strip() for j in junk_notes.split(',') if j.strip()]

                    for f in files:
                        f_id = f['id']
                        f_name = f['name']
                        is_junk = any(junk in f_name for junk in junk_keywords)

                        if is_junk:
                            drive_service.files().delete(fileId=f_id).execute()
                        else:
                            new_name = rename_dict.get(f_name, f_name)
                            update_body = {'name': new_name} if new_name != f_name else None
                            
                            drive_service.files().update(
                                fileId=f_id,
                                addParents=target_id,
                                removeParents=staging_id,
                                body=update_body
                            ).execute()

                    drive_service.files().delete(fileId=staging_id).execute()
                    queue_ws.update_cell(idx, 5, "Completed")
                    queue_ws.update_cell(idx, 8, "Relocated, Renamed & Purged")

                except Exception as e:
                    print(f"Relocation Error for Row {idx}: {e}")
                    
    except Exception as e:
        print(f"Relocation Engine Failed: {e}")

# Module 7: Master Cleanup Engine (Step 5)
def execute_staging_cleanup(gc, drive_service):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID)

        # 1. SHORT-FORM: TOTAL WIPE PROTOCOL
        try:
            short_ws = sheet.worksheet("Short_Tracker")
            short_records = short_ws.get_all_records()
            for c_idx, row in enumerate(short_records, start=2):
                folder_id = str(row.get("Folder ID", "")).strip()
                video_link = str(row.get("Video Link", "")).strip() 
                if folder_id and video_link.startswith("http"):
                    try:
                        drive_service.files().delete(fileId=folder_id).execute()
                    except Exception:
                        pass
        except Exception as e:
            print(f"Short Tracker Cleanup Error: {e}")

        # 2. LONG-FORM: SURGICAL WIPE PROTOCOL
        try:
            long_ws = sheet.worksheet("Long_Tracker")
            long_records = long_ws.get_all_records()
            for c_idx, row in enumerate(long_records, start=2):
                folder_id = str(row.get("Folder ID", "")).strip()
                status = str(row.get("Status", "")).strip().upper()
                chapter_name = str(row.get("Chapter Name", "")).strip()
                safe_chapter = chapter_name.replace('.', '_').replace(' ', '_')

                if folder_id and status in ["DONE", "DELIVERED", "POSTED"]:
                    try:
                        query = f"'{folder_id}' in parents and trashed = false"
                        results = drive_service.files().list(q=query, fields="files(id, name)", pageSize=1000).execute()
                        for f in results.get('files', []):
                            fname = f['name']
                            if fname.endswith('.json') or '_map' in fname:
                                drive_service.files().delete(fileId=f['id']).execute()
                    except Exception:
                        pass
                    try:
                        vault_query = "name = 'Nexus_Script_Vault' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                        vault_results = drive_service.files().list(q=vault_query, fields="files(id)").execute()
                        if vault_results.get('files'):
                            vault_id = vault_results['files'][0]['id']
                            script_filename = f"script_{safe_chapter}.json"
                            script_query = f"'{vault_id}' in parents and name = '{script_filename}' and trashed = false"
                            script_results = drive_service.files().list(q=script_query, fields="files(id)").execute()
                            for sf in script_results.get('files', []):
                                drive_service.files().delete(fileId=sf['id']).execute()
                    except Exception:
                        pass
        except Exception as e:
            print(f"Long Tracker Cleanup Error: {e}")
    except Exception as e:
        print(f"Master Cleanup Engine Failed: {e}")

# Module 8: Main Workflow Engine
def process_queue():
    gc, drive_service = get_google_services()
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    records = queue_ws.get_all_records()
    session = create_resilient_session()

    for idx, row in enumerate(records, start=2):
        status_key = next((k for k in row.keys() if "Download Status" in k), "Download Status")
        status = str(row.get(status_key, "")).strip().title()
        
        if status != "Pending":
            continue
        
        series_name = row.get("Series Title", "")
        chapter_num = row.get("Chapter Number", "")
        chapter_url = row.get("Direct Chapter Web URL", "")
        error_msg = "Link doesn't work, find a new better link"
        
        if not chapter_url or not str(chapter_url).startswith("http"):
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 8, error_msg)
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)
        
        panel_urls = extract_panels_with_playwright(chapter_url)
        
        if not panel_urls:
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 8, error_msg)
            continue
        
        success_count = 0
        
        # Tier 1: Smart Naming (Preserve Numbers, Rename Hashes)
        for seq_idx, p_url in enumerate(panel_urls, start=1):
            parsed = urlparse(p_url)
            original_fname = os.path.basename(parsed.path)
            base_name, ext = os.path.splitext(original_fname)
            
            # Normalize extension just in case URL formatting is weird
            if not ext or ext.lower() not in ['.jpg', '.jpeg', '.png', '.webp']:
                if ".png" in p_url.lower(): ext = ".png"
                elif ".webp" in p_url.lower(): ext = ".webp"
                else: ext = ".jpg"

            # STRICT CHECK: If the name is pure numbers (e.g., "01", "002"), keep it.
            # If it contains letters (e.g., "cf6105", "page_1"), rename it sequentially.
            if base_name.isdigit():
                final_fname = f"{base_name}{ext}"
            else:
                final_fname = f"{seq_idx:03d}{ext}"
            
            target_path = os.path.join(local_dir, final_fname)
            
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
            
            queue_ws.update_cell(idx, 5, "Downloaded")
            queue_ws.update_cell(idx, 6, uploaded_id)
            queue_ws.update_cell(idx, 8, "Awaiting Assistant Audit")
        else:
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 8, error_msg)
            
    execute_relocation(gc, drive_service)
    execute_staging_cleanup(gc, drive_service)

if __name__ == "__main__":
    process_queue()
