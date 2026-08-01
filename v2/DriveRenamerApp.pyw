"""
Drive File Renamer + Downloader App (Local-Only Rename)
-----------------------------------------------------------
Paste a Google Drive folder link, choose a destination folder on your PC,
click "Rename & Download", and this app will:
  1. Read all files in that Drive folder (correct chronological order,
     using filename timestamps when available)
  2. Download every file straight to the folder you picked, with a
     sequential number added to the filename: (1), (2), (3)...

IMPORTANT: This version NEVER modifies anything on Google Drive itself -
no renaming happens on Drive, only download + local renaming on your PC.
This means it works even with Viewer-only (view/download) access, and
never touches files shared by someone else.

First-time use: a browser window will open asking you to log in to Google
and approve access. After that, it remembers you (via token.json) and
won't ask again unless the token expires.

Downloads happen one file at a time (not simultaneously), and files
already downloaded before are automatically skipped on future runs.
"""

import os
import re
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Folder where this script lives (so it works no matter where you double-click it from)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")

# Automatically find the client_secret_*.json file in this same folder
def find_credentials_file():
    for name in os.listdir(BASE_DIR):
        if name.startswith("client_secret_") and name.endswith(".json"):
            return os.path.join(BASE_DIR, name)
    return None


def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds_file = find_credentials_file()
            if not creds_file:
                raise FileNotFoundError(
                    "Hindi mahanap yung client_secret_*.json file sa parehong "
                    "folder ng app na ito. Siguraduhing andun siya."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def extract_folder_id(link_or_id: str) -> str:
    """Accepts either a full Drive folder URL or a bare folder ID."""
    link_or_id = link_or_id.strip()
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", link_or_id)
    if match:
        return match.group(1)
    # Might already just be a raw ID
    if re.fullmatch(r"[a-zA-Z0-9_-]+", link_or_id):
        return link_or_id
    raise ValueError("Hindi ma-detect ang Folder ID mula sa link na yan.")


ALREADY_NUMBERED = re.compile(r"^\(\d+\)")

# Pattern para sa mga filename tulad ng 20260721_122820.mp4 (YYYYMMDD_HHMMSS)
FILENAME_TIMESTAMP_PATTERN = re.compile(r"(\d{8})_(\d{6})")


def extract_filename_timestamp(filename: str):
    """Kunin ang petsa/oras mula sa pangalan ng file mismo (hal. 20260721_122820),
    na mas tumpak kaysa sa Drive's createdTime (na maaring iba dahil sa
    upload order, hindi sa tunay na oras ng pagkuha ng video/photo).
    Nagbabalik ng sortable string, o None kung walang match."""
    match = FILENAME_TIMESTAMP_PATTERN.search(filename)
    if match:
        return match.group(1) + match.group(2)  # e.g. "20260721122820"
    return None


# Generic pattern para sa ibang camera/device na hindi gumagamit ng phone-style
# timestamp (hal. IMG_1333, ACHR_74858, DSC_0001, C0042, MVI_0001).
# Kumukuha ng letter prefix + number, para ma-group ayon sa prefix at
# ma-sort ayon sa number - mas predictable kaysa sa Drive's createdTime.
GENERIC_PREFIX_NUMBER_PATTERN = re.compile(r"^([A-Za-z_\-]*)(\d+)")


def extract_generic_prefix_number(filename: str):
    """Kunin ang letter prefix + number mula sa filename bilang pangalawang
    antas ng sorting fallback. Nagbabalik ng (prefix, number) tuple, o None
    kung walang match."""
    match = GENERIC_PREFIX_NUMBER_PATTERN.match(filename)
    if match:
        prefix = match.group(1).upper()
        number = int(match.group(2))
        return (prefix, number)
    return None


def sort_key_for_file(f):
    """3 antas ng sorting, mula sa pinaka-maaasahan hanggang huling fallback:
    1. Phone-style timestamp sa filename (hal. 20260721_122820) - pinaka-tumpak
    2. Generic letter-prefix + number pattern (hal. IMG_1333, ACHR_74858) -
       i-group ayon sa prefix, i-sort ayon sa number - predictable kahit
       ibang camera/device ang ginamit
    3. Drive's createdTime - huling fallback na lang kung wala talagang
       matukoy na pattern sa pangalan"""
    filename_ts = extract_filename_timestamp(f["name"])
    if filename_ts:
        return (0, filename_ts, 0)

    generic = extract_generic_prefix_number(f["name"])
    if generic:
        prefix, number = generic
        return (1, prefix, number)

    return (2, f.get("createdTime", ""), 0)


def download_file(service, file_id, dest_path, log_prefix, log):
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log(f"{log_prefix} ... {pct}%")


def rename_and_download_folder(folder_id: str, dest_folder: str, log, reset_numbering=False):
    service = get_drive_service()

    files = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, createdTime, mimeType)",
            pageSize=200,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if not files:
        log("Walang nakitang files sa folder na yan.")
        return

    # I-sort base sa timestamp mula sa filename (mas tumpak), fallback sa
    # Drive's createdTime lang kung walang match sa pangalan
    files.sort(key=sort_key_for_file)

    log(f"Nakita: {len(files)} files.\n")
    log("Hindi ito magbabago ng kahit anong file sa Drive - local rename lang, "
        "download-only mula sa Drive side.\n")

    # I-check kung anong mga orihinal na filename ang meron nang local copy
    # (base sa pangalan pagkatapos ng "(N)" prefix), para malaman kung alin
    # ang skip-in, at kung ano ang susunod na available number
    existing_local_names = set()
    existing_numbers = []
    if os.path.isdir(dest_folder):
        for local_name in os.listdir(dest_folder):
            m = ALREADY_NUMBERED.match(local_name)
            if m:
                try:
                    existing_numbers.append(int(re.match(r"^\((\d+)\)", local_name).group(1)))
                except Exception:
                    pass
                existing_local_names.add(ALREADY_NUMBERED.sub("", local_name, count=1))

    if reset_numbering:
        log("RESET MODE: Ire-renumber ulit ang LOCAL na pangalan base sa tamang "
            "pagkakasunod-sunod (hindi pa rin magbabago ang Drive).\n")
        next_number = 1
        existing_local_names = set()  # ituring lahat na parang bago pa lang
    else:
        next_number = max(existing_numbers, default=0) + 1

    processed_count = 0
    for f in files:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            log(f"Skip (subfolder, hindi file): {f['name']}")
            continue

        original_name = f["name"]

        if not reset_numbering and original_name in existing_local_names:
            log(f"Skip (na-download na dati): {original_name}")
            continue

        final_name = f"({next_number}){original_name}"
        next_number += 1

        dest_path = os.path.join(dest_folder, final_name)
        if os.path.exists(dest_path):
            log(f"Skip (na-download na dati): {final_name}")
        else:
            download_file(service, f["id"], dest_path, final_name, log)
        processed_count += 1

    log(f"\nTapos na! {processed_count} files ang na-proseso, na-save sa:\n{dest_folder}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("Drive File Renamer + Downloader")
        root.geometry("600x500")
        root.configure(bg="#1e1e1e")

        label = tk.Label(
            root, text="I-paste ang Google Drive folder link:",
            bg="#1e1e1e", fg="white", font=("Segoe UI", 11)
        )
        label.pack(pady=(15, 5))

        self.link_entry = tk.Entry(root, width=65, font=("Segoe UI", 10))
        self.link_entry.pack(pady=5)

        folder_label = tk.Label(
            root, text="Saan i-se-save sa PC mo:",
            bg="#1e1e1e", fg="white", font=("Segoe UI", 11)
        )
        folder_label.pack(pady=(15, 5))

        folder_row = tk.Frame(root, bg="#1e1e1e")
        folder_row.pack(pady=5)

        self.dest_folder = tk.StringVar()
        self.dest_entry = tk.Entry(
            folder_row, width=50, font=("Segoe UI", 10),
            textvariable=self.dest_folder, state="readonly"
        )
        self.dest_entry.pack(side="left", padx=(0, 8))

        browse_btn = tk.Button(
            folder_row, text="Choose Folder", command=self.choose_folder,
            bg="#374151", fg="white", font=("Segoe UI", 9, "bold"),
            padx=8, pady=4, relief="flat"
        )
        browse_btn.pack(side="left")

        self.rename_btn = tk.Button(
            root, text="Rename & Download", command=self.on_rename_click,
            bg="#3b82f6", fg="white", font=("Segoe UI", 11, "bold"),
            padx=10, pady=6, relief="flat"
        )
        self.rename_btn.pack(pady=(15, 5))

        self.reset_var = tk.BooleanVar(value=False)
        reset_check = tk.Checkbutton(
            root, text="Reset numbering (ayusin ang lumang maling pagkakasunod-sunod)",
            variable=self.reset_var, bg="#1e1e1e", fg="white",
            selectcolor="#1e1e1e", font=("Segoe UI", 9), activebackground="#1e1e1e",
            activeforeground="white"
        )
        reset_check.pack(pady=(0, 10))

        self.log_box = scrolledtext.ScrolledText(
            root, width=72, height=16, bg="#111111", fg="#00ff88",
            font=("Consolas", 9)
        )
        self.log_box.pack(padx=10, pady=10)

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Piliin ang destination folder")
        if folder:
            self.dest_folder.set(folder)

    def log(self, message):
        self.log_box.insert(tk.END, message + "\n")
        self.log_box.see(tk.END)
        self.root.update_idletasks()

    def on_rename_click(self):
        link = self.link_entry.get().strip()
        dest = self.dest_folder.get().strip()

        if not link:
            messagebox.showwarning("Kulang", "Paste muna ng Drive folder link.")
            return
        if not dest:
            messagebox.showwarning("Kulang", "Pumili muna ng destination folder.")
            return

        self.rename_btn.config(state="disabled", text="Nagpoproseso...")
        self.log_box.delete("1.0", tk.END)

        def run():
            try:
                folder_id = extract_folder_id(link)
                self.log(f"Folder ID: {folder_id}")
                self.log(f"Destination: {dest}\n")
                rename_and_download_folder(folder_id, dest, self.log, reset_numbering=self.reset_var.get())
            except Exception as e:
                self.log(f"\nERROR: {e}")
                messagebox.showerror("May Error", str(e))
            finally:
                self.rename_btn.config(state="normal", text="Rename & Download")

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
