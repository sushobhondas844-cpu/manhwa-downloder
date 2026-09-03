import json
import os
import re
import time
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import gspread
from PIL import Image
from playwright.sync_api import sync_playwright
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SPREADSHEET_ID = "1Ww6sDCL8gMoJxdwOwz3kw0YIedQEdAkSNoRfj0U3wDk"
MASTER_CONTROLLER_ID = "1H6lOh8Z_TZED6cV4FiZyodRajgdGy7DWv8Bf671YKHc"
RAW_STAGING_FOLDER_ID = "1EGeKmI9y_7iV3Wu9LuTEJLk_5vTgZXpk"
SHORT_FORM_ROOT_ID = "1M7g4zbm8dtksXH1nw70j8WETYMnhBwL6"
LONG_FORM_ROOT_ID = "14lqpLT4lzAUAW6M0HLCLhF8cFadHtMm8"
SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# Module 1: Authentication Engine
def get_google_services():
    sa_json_str = os.environ.get("NEXUS_DRIVE_CREDS")
    sa_info = json.loads(sa_json_str)
    sheet_creds = SACredentials.from_service_account_info(
        sa_info, scopes=SHEET_SCOPES
    )
    gc = gspread.authorize(sheet_creds)

    oauth_json_str = os.environ.get("DRIVE_OAUTH_TOKEN")
    oauth_info = json.loads(oauth_json_str)
    drive_creds = OAuthCredentials(
        token=oauth_info.get("token"),
        refresh_token=oauth_info.get("refresh_token"),
        token_uri=oauth_info.get("token_uri"),
        client_id=oauth_info.get("client_id"),
        client_secret=oauth_info.get("client_secret"),
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
        allowed_methods=["GET"],
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


DOMAIN_PRESETS = {
    "asurascans.com": 'img[alt*="Page"], div[class*="chapter"] img',
    "demonicscans.org": "#readerarea img",
    "mangadex.org": ".reader--container img",
    "hivetoons.org": ".reading-content img",
    "en-thunderscans.com": "#readerarea img",
}

def extract_panels_with_playwright(chapter_url, local_dir, custom_selector=""):
    success_count = 0
    captured_urls = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1080})

        try:
            print(f"[DEBUG] Navigating to: {chapter_url}")
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
            page.screenshot(path="debug_browser_view.png")
            print("[DEBUG] Saved debug_browser_view.png")
        except Exception as e:
            print(f"[DEBUG] Navigation crashed: {e}")
            browser.close()
            return 0, "Navigation crashed or timed out."

        page_title = page.title().lower()
        if "just a moment" in page_title or "attention required" in page_title or "turnstile" in page.content().lower():
            browser.close()
            return 0, "Cloudflare protection active. Use a different website."

        for _ in range(5):
            page.evaluate("window.scrollBy(0, 1500)")
            time.sleep(0.4)
        page.wait_for_timeout(2000)

        selector = custom_selector.strip()
        if not selector:
            for domain, preset in DOMAIN_PRESETS.items():
                if domain in chapter_url.lower():
                    selector = preset
                    break

        image_elements = []
        if selector:
            image_elements = page.query_selector_all(selector)

        if not image_elements:
            for fallback in ["#readerarea img", ".reading-content img", ".entry-content img"]:
                image_elements = page.query_selector_all(fallback)
                if image_elements:
                    break

        if not image_elements:
            image_elements = page.query_selector_all("img")

        if not image_elements:
            browser.close()
            return 0, "Container missing. Please update CSS selector."

        seq_idx = 1
        for img in image_elements:
            url = (img.get_attribute("src") or 
                   img.get_attribute("data-src") or 
                   img.get_attribute("data-lazy-src") or 
                   img.get_attribute("data-original"))
                   
            if not url or not url.startswith("http"):
                continue

            url_lower = url.lower()
            is_valid_ext = any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
            is_junk = any(
                noise in url_lower
                for noise in [
                    "avatar", "user", "users", "logo", "/icon/", "badge", 
                    "banner", "/ads/", "discord", "cover", "comment", 
                    "sidebar", "widget", "sponsor", "paypal", "patreon"
                ]
            )

            if is_valid_ext and not is_junk:
                if url not in captured_urls:
                    captured_urls.append(url)

                    parsed = urlparse(url)
                    original_fname = os.path.basename(parsed.path)
                    base_name, ext = os.path.splitext(original_fname)

                    if not ext or ext.lower() not in [".jpg", ".jpeg", ".png", ".webp"]:
                        ext = ".webp" if ".webp" in url_lower else ".jpg"

                    if base_name.isdigit():
                        final_fname = f"{int(base_name):03d}{ext}"
                    else:
                        final_fname = f"{seq_idx:03d}{ext}"

                    target_path = os.path.join(local_dir, final_fname)

                    try:
                        resp = page.request.get(url, headers={"Referer": chapter_url}, timeout=25000)
                        if resp.status == 200:
                            image_bytes = resp.body()
                            if verify_and_save_image(image_bytes, target_path):
                                success_count += 1
                                seq_idx += 1
                    except Exception:
                        pass
                    time.sleep(0.05)

        browser.close()
        
        if success_count == 0:
            return 0, "Images found but download blocked or geometric check failed."
            
    return success_count, "Success"

# Module 4: Verification Engine
# FIX: Removed min_size_kb filter completely so small story panels aren't skipped
# Module 4: Dynamic Geometric Verification Engine
def verify_and_save_image(image_bytes, target_path):
    try:
        from io import BytesIO
        # Open in RAM to check dimensions first
        with Image.open(BytesIO(image_bytes)) as img:
            img.verify() 
        
        with Image.open(BytesIO(image_bytes)) as img:
            width, height = img.size
            
            # RULE 1: Reject small icons, avatars, and UI buttons (too narrow or too short)
            if width < 400 or height < 300:
                return False
                
            # RULE 2: Reject horizontal banner ads (extremely wide but short)
            if width > (height * 2.5):
                return False

        # If it matches the shape of a tall story panel, save it
        with open(target_path, "wb") as f:
            f.write(image_bytes)
        return True

    except Exception:
        if os.path.exists(target_path):
            os.remove(target_path)
        return False


# Module 5: Drive Ingestion Engine
def upload_folder_to_drive(drive_service, local_folder, folder_name, parent_id):
    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = (
        drive_service.files().create(body=file_metadata, fields="id").execute()
    )
    created_id = folder.get("id")

    for fname in sorted(os.listdir(local_folder)):
        fpath = os.path.join(local_folder, fname)
        if os.path.isfile(fpath):
            mime = "image/webp" if fname.endswith(".webp") else "image/jpeg"
            media = MediaFileUpload(fpath, mimetype=mime, resumable=True)
            f_metadata = {"name": fname, "parents": [created_id]}
            drive_service.files().create(
                body=f_metadata, media_body=media, fields="id"
            ).execute()
    return created_id


# Module 5.1: Dynamic Directory Traversal Engine
def normalize_folder_name(name):
    """Normalizes spaces, underscores, and case for fuzzy folder matching."""
    return re.sub(r"[\s_]+", " ", name).strip().lower()

# Module 5.1: Dynamic Directory Traversal & Sheet Sync Engine
def get_or_create_drive_path(drive_service, root_id, path_list):
    """Fallback 1-tier crawler used exclusively for Short-Form manhwa."""
    current_parent_id = root_id
    for folder_name in path_list:
        safe_name = folder_name.replace("'", "\\'")
        query = (
            f"'{current_parent_id}' in parents and name = '{safe_name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        results = drive_service.files().list(
            q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        files = results.get("files", [])
        if files:
            current_parent_id = files[0]["id"]
        else:
            meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [current_parent_id]}
            folder = drive_service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
            current_parent_id = folder.get("id")
    return current_parent_id

def get_or_create_target_folder(gc, drive_service, series_name, chapter_num, is_short=True):
    clean_series = series_name.strip()
    
    if is_short:
        return get_or_create_drive_path(drive_service, SHORT_FORM_ROOT_ID, [clean_series])
        
    # --- LONG FORM: DATABASE-DRIVEN RESOLUTION ---
    tracker_sheet = gc.open_by_key(SPREADSHEET_ID)
    long_ws = tracker_sheet.worksheet("Long_Form_Tracker")
    long_records = long_ws.get_all_records()
    
    series_folder_id = ""
    row_idx = None
    
    # 1. Match 'Title' or 'Series Title' in Long_Form_Tracker
    for idx, row in enumerate(long_records, start=2):
        title_val = str(row.get("Title") or row.get("Series Title") or "").strip()
        if title_val.lower() == clean_series.lower():
            row_idx = idx
            series_folder_id = str(row.get("Folder ID", "")).strip()
            break
            
    # 2. Check Drive under LONG_FORM_ROOT_ID first before creating
    if not series_folder_id:
        safe_series = clean_series.replace("'", "\\'")
        drive_query = (
            f"'{LONG_FORM_ROOT_ID}' in parents and name = '{safe_series}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        drive_results = drive_service.files().list(
            q=drive_query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        drive_files = drive_results.get("files", [])
        
        if drive_files:
            series_folder_id = drive_files[0]["id"]
        else:
            meta = {"name": clean_series, "mimeType": "application/vnd.google-apps.folder", "parents": [LONG_FORM_ROOT_ID]}
            folder = drive_service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
            series_folder_id = folder.get("id")
            
        # Sync found/created Folder ID back to Long_Form_Tracker (Column O)
        if row_idx:
            headers = long_ws.row_values(1)
            col_idx = headers.index("Folder ID") + 1 if "Folder ID" in headers else 15
            long_ws.update_cell(row_idx, col_idx, series_folder_id)

    # 3. Resolve Chapter Subfolder
    chapter_name = f"{clean_series} Chapter {chapter_num}"
    safe_chapter_name = chapter_name.replace("'", "\\'")
    
    query = (
        f"'{series_folder_id}' in parents and name = '{safe_chapter_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    results = drive_service.files().list(
        q=query, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    
    files = results.get("files", [])
    if files:
        return files[0]["id"]
        
    chapter_meta = {"name": chapter_name, "mimeType": "application/vnd.google-apps.folder", "parents": [series_folder_id]}
    chapter_folder = drive_service.files().create(body=chapter_meta, fields="id", supportsAllDrives=True).execute()
    return chapter_folder.get("id")

# Module 6: Relocation Engine
# Module 6: Relocation Engine
def execute_relocation(gc, drive_service):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID)
        queue_ws = sheet.worksheet("Download_Queue")
        records = queue_ws.get_all_records()

        for idx, row in enumerate(records, start=2):
            status = str(row.get("Download Status", "")).strip().upper()

            if status == "READY TO MOVE":
                staging_id = str(row.get("Raw Staging Folder ID", "")).strip()
                series_name = str(row.get("Series Title", "")).strip()
                chapter_num = str(row.get("Chapter Number", "")).strip()
                format_type = str(row.get("Format (Long / Short)", "")).strip().lower()
                is_short = "short" in format_type
                
                rename_map_raw = str(row.get("Panel Sequence & Rename Map", "")).strip()
                junk_notes = str(row.get("Link Notes", "")).strip()

                if not staging_id:
                    continue

                try:
                    # PASS GC TO TARGET RESOLVER
                    target_folder_id = get_or_create_target_folder(
                        gc, drive_service, series_name, chapter_num, is_short
                    )

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
                    files = results.get("files", [])

                    for f in files:
                        f_id = f['id']
                        f_name = f['name']
                        
                        junk_keywords = [j.strip() for j in junk_notes.split(',') if j.strip()]
                        is_junk = any(junk in f_name for junk in junk_keywords)

                        if is_junk:
                            drive_service.files().delete(fileId=f_id).execute()
                            continue
                        
                        new_name = rename_dict.get(f_name, f_name)
                        update_body = {'name': new_name} if new_name != f_name else None
                        
                        drive_service.files().update(
                            fileId=f_id,
                            addParents=target_folder_id,
                            removeParents=staging_id,
                            body=update_body,
                            supportsAllDrives=True
                        ).execute()

                    drive_service.files().delete(fileId=staging_id).execute()

                    queue_ws.update_cell(idx, 5, "Sorted & Relocated")
                    queue_ws.update_cell(idx, 6, "")
                    queue_ws.update_cell(idx, 10, "Ready for Processing")
                    
                    # --- TWO-WAY COUNTER SYNCHRONIZATION ---
                    # --- TWO-WAY COUNTER SYNCHRONIZATION ---
                    if not is_short:
                        try:
                            # 1. Update Long_Form_Tracker (Column F)
                            long_ws = sheet.worksheet("Long_Form_Tracker")
                            long_records = long_ws.get_all_records()
                            for l_idx, l_row in enumerate(long_records, start=2):
                                l_title = str(l_row.get("Title") or l_row.get("Series Title") or "").strip()
                                if l_title.lower() == series_name.strip().lower():
                                    curr_dl = l_row.get("Downloaded Chapters", 0)
                                    new_dl = int(curr_dl) + 1 if str(curr_dl).isdigit() else 1
                                    headers = long_ws.row_values(1)
                                    col_dl = headers.index("Downloaded Chapters") + 1 if "Downloaded Chapters" in headers else 6
                                    long_ws.update_cell(l_idx, col_dl, new_dl)
                                    break
                                    
                            # 2. Update Master Controller (Column B)
                            master_sheet = gc.open_by_key(MASTER_CONTROLLER_ID).sheet1
                            m_records = master_sheet.get_all_records()
                            for m_idx, m_row in enumerate(m_records, start=2):
                                if str(m_row.get("Name", "")).strip().lower() == series_name.strip().lower():
                                    curr_tot = m_row.get("Total Chapters", 0)
                                    new_tot = int(curr_tot) + 1 if str(curr_tot).isdigit() else 1
                                    master_sheet.update_cell(m_idx, 2, new_tot) # Col B is index 2
                                    break
                        except Exception as sync_err:
                            print(f"[DEBUG] Failed to update chapter counters: {sync_err}")

                except Exception as e:
                    print(f"Relocation Error for Row {idx} ({series_name}): {e}")

    except Exception as e:
        print(f"Relocation Engine Failed: {e}")


# Module 7: Master Cleanup Engine (Step 5)
# FIX: Fully restored to safely purge Shorts and Long-Form junk after delivery
def execute_staging_cleanup(gc, drive_service):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID)

        # 1. SHORT-FORM CLEANUP
        try:
            short_ws = sheet.worksheet("Shorts_Tracker")
            short_records = short_ws.get_all_records()
            for c_idx, row in enumerate(short_records, start=2):
                folder_id = str(row.get("Short Working Folder ID", "")).strip()
                video_link = str(row.get("YouTube Shorts Link", "")).strip()
                if folder_id and video_link.startswith("http"):
                    try:
                        drive_service.files().delete(fileId=folder_id).execute()
                    except Exception:
                        pass
        except Exception as e:
            print(f"Shorts Tracker Cleanup Error: {e}")

        # 2. LONG-FORM SCRIPT/MAP CLEANUP
        try:
            long_ws = sheet.worksheet("Long_Form_Tracker")
            long_records = long_ws.get_all_records()
            for c_idx, row in enumerate(long_records, start=2):
                folder_id = str(row.get("Folder ID", "")).strip()
                status = str(row.get("Long-Form Video Status", "")).strip().upper()
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
            print(f"Long-Form Tracker Cleanup Error: {e}")

    except Exception as e:
        print(f"Master Cleanup Engine Failed: {e}")


# Module 8: Main Workflow Engine
def process_queue():
    gc, drive_service = get_google_services()
    sheet = gc.open_by_key(SPREADSHEET_ID)
    queue_ws = sheet.worksheet("Download_Queue")
    records = queue_ws.get_all_records()

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Download Status", "")).strip().title()

        if status != "Pending":
            continue

        series_name = row.get("Series Title", "")
        chapter_num = row.get("Chapter Number", "")
        chapter_url = row.get("Direct Chapter Web URL", "")
        
        if not chapter_url or not str(chapter_url).startswith("http"):
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 10, "Link doesn't work, find a new better link")
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(" ", "_")
        os.makedirs(local_dir, exist_ok=True)

        custom_css = str(row.get("Custom CSS Selector", "")).strip()

        success_count, error_reason = extract_panels_with_playwright(chapter_url, local_dir, custom_selector=custom_css)

        if success_count > 0:
            gdrive_name = f"{series_name}_Chapter_{chapter_num}".replace(" ", "_")
            uploaded_id = upload_folder_to_drive(
                drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID
            )

            queue_ws.update_cell(idx, 5, "Downloaded")
            queue_ws.update_cell(idx, 6, uploaded_id)
            queue_ws.update_cell(idx, 10, "Awaiting Assistant Audit")
        else:
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 10, error_reason)
    # Note: Relocation and Cleanup are deliberately decoupled from the process_queue loop


if __name__ == "__main__":
    # 1. Download pending links
    process_queue()
    
    # 2. Relocate audited folders and run post-publish cleanup
    gc, drive_service = get_google_services()
    execute_relocation(gc, drive_service)
    execute_staging_cleanup(gc, drive_service)
