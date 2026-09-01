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


# Module 3: Strict Story Panel Interception
def extract_panels_with_playwright(chapter_url):
    captured_urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            viewport={"width": 1280, "height": 1080},
        )
        page = context.new_page()

        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            browser.close()
            return []

        # Scroll to force lazy loading
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

        page.wait_for_timeout(2000)

        # FIX: Target reader area strictly to exclude noise. No fallback to full page.
        reader_selectors = [
            "#readerarea",
            ".reading-content",
            ".page-break",
            ".entry-content",
        ]
        target_container = None

        for selector in reader_selectors:
            if page.query_selector(selector):
                target_container = page.query_selector(selector)
                break

        # STRICT ABORT: If no comic container is found, abort to prevent downloading 200+ junk images
        if not target_container:
            browser.close()
            return []

        # Query all images ONLY inside the comic container
        image_elements = target_container.query_selector_all("img")

        for img in image_elements:
            url = img.get_attribute("src") or img.get_attribute("data-src")
            if not url or not url.startswith("http"):
                continue

            url_lower = url.lower()

            # FIX: Explicitly require chapter pathways and ban avatars/covers/users
            is_valid_ext = any(
                ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp"]
            )
            
            # SMART ENGINE: Comprehensive negative filter to ban UI junk elements
            is_junk = any(
                noise in url_lower
                for noise in [
                    "avatar", "user", "users", "logo", "icon", "badge", 
                    "banner", "ads", "discord", "cover", "comment", 
                    "sidebar", "widget", "sponsor", "paypal", "patreon","profiles"
                ]
            )

            # Because we already isolated the 'target_container' (the comic reader area),
            # any valid image format inside it that IS NOT explicitly junk is safely assumed to be a panel.
            if is_valid_ext and not is_junk:
                if url not in captured_urls:
                    captured_urls.append(url)


# Module 4: Verification Engine
# FIX: Removed min_size_kb filter completely so small story panels aren't skipped
def verify_and_save_image(image_bytes, target_path):
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


# Helper with escaped single quotes for Google Drive queries
def get_or_create_target_folder(
    drive_service, series_name, chapter_num, is_short=True
):
    parent_id = SHORT_FORM_ROOT_ID if is_short else LONG_FORM_ROOT_ID
    target_name = f"{series_name}_Chapter_{chapter_num}".replace(" ", "_")
    
    # FIX: Escape single quotes so titles like "The Berserker's" don't trigger Error 400
    safe_target_name = target_name.replace("'", "\\'")

    query = (
        f"'{parent_id}' in parents and name = '{safe_target_name}' and mimeType ="
        " 'application/vnd.google-apps.folder' and trashed = false"
    )
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])

    if files:
        return files[0]["id"]

    meta = {
        "name": target_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive_service.files().create(body=meta, fields="id").execute()
    return folder.get("id")


# Module 6: Relocation Engine
def execute_relocation(gc, drive_service):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID)
        queue_ws = sheet.worksheet("Download_Queue")
        records = queue_ws.get_all_records()

        for idx, row in enumerate(records, start=2):
            status = str(row.get("Download Status", "")).strip().upper()

            # Strict 2-Step enforcement: Only move if manually marked READY TO MOVE
            if status == "READY TO MOVE":
                staging_id = str(row.get("Raw Staging Folder ID", "")).strip()
                series_name = str(row.get("Series Title", "")).strip()
                chapter_num = str(row.get("Chapter Number", "")).strip()
                format_type = str(row.get("Format (Long / Short)", "")).strip().lower()
                is_short = "short" in format_type
                rename_map_raw = str(row.get("Panel Sequence & Rename Map", "")).strip()

                if not staging_id:
                    continue

                try:
                    target_folder_id = get_or_create_target_folder(
                        drive_service, series_name, chapter_num, is_short
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
                    results = (
                        drive_service.files()
                        .list(q=query, fields="files(id, name)")
                        .execute()
                    )
                    files = results.get("files", [])

                    for f in files:
                        f_id = f['id']
                        f_name = f['name']
                        
                        # Apply rename mapping if the filename exists in the dictionary
                        new_name = rename_dict.get(f_name, f_name)
                        update_body = {'name': new_name} if new_name != f_name else None
                        
                        drive_service.files().update(
                            fileId=f_id,
                            addParents=target_folder_id,
                            removeParents=staging_id,
                            body=update_body
                        ).execute()

                    # Delete empty staging folder after move
                    drive_service.files().delete(fileId=staging_id).execute()

                    # Update Download Status to Sorted & Relocated and clear staging ID
                    queue_ws.update_cell(idx, 5, "Sorted & Relocated")
                    queue_ws.update_cell(idx, 6, "")
                    queue_ws.update_cell(idx, 9, "Ready for Processing")
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
    session = create_resilient_session()

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Download Status", "")).strip().title()

        if status != "Pending":
            continue

        series_name = row.get("Series Title", "")
        chapter_num = row.get("Chapter Number", "")
        chapter_url = row.get("Direct Chapter Web URL", "")
        error_msg = "Link doesn't work, find a new better link"

        if not chapter_url or not str(chapter_url).startswith("http"):
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 9, error_msg)
            continue

        local_dir = f"./temp_downloads/{series_name}_Ch{chapter_num}".replace(
            " ", "_"
        )
        os.makedirs(local_dir, exist_ok=True)

        panel_urls = extract_panels_with_playwright(chapter_url)

        if not panel_urls:
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 9, error_msg)
            continue

        success_count = 0

        # Smart Naming: Keep chronological numbers, rename hashes
        for seq_idx, p_url in enumerate(panel_urls, start=1):
            parsed = urlparse(p_url)
            original_fname = os.path.basename(parsed.path)
            base_name, ext = os.path.splitext(original_fname)

            if not ext or ext.lower() not in [".jpg", ".jpeg", ".png", ".webp"]:
                ext = ".webp" if ".webp" in p_url.lower() else ".jpg"

            # Check if name is purely numerical to avoid altering natively clean names
            if base_name.isdigit():
                final_fname = f"{int(base_name):03d}{ext}"
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
            gdrive_name = (
                f"{series_name}_Chapter_{chapter_num}".replace(" ", "_")
            )
            uploaded_id = upload_folder_to_drive(
                drive_service, local_dir, gdrive_name, RAW_STAGING_FOLDER_ID
            )

            # FIX: Only set to Downloaded. Forces the 2-step workflow so you can audit it.
            queue_ws.update_cell(idx, 5, "Downloaded")
            queue_ws.update_cell(idx, 6, uploaded_id)
            queue_ws.update_cell(idx, 9, "Awaiting Assistant Audit")
        else:
            queue_ws.update_cell(idx, 5, "Error")
            queue_ws.update_cell(idx, 9, error_msg)

    # Note: Relocation and Cleanup are deliberately decoupled from the process_queue loop


if __name__ == "__main__":
    # 1. Download pending links
    process_queue()
    
    # 2. Relocate audited folders and run post-publish cleanup
    gc, drive_service = get_google_services()
    execute_relocation(gc, drive_service)
    execute_staging_cleanup(gc, drive_service)
