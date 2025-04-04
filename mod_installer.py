import os
import platform
import uuid
import customtkinter as ctk
from tkinter import messagebox, simpledialog
import gdown
import threading
import time
import logging
import subprocess
import requests  # For HTTP requests to the API server

# === Windows-Specific Imports for Documents Folder Detection ===
if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

# === CONFIGURATION ===
GOOGLE_SHEET_NAME = "ETS2_Mod_Data"  # (No longer used directly in this file)
API_URL = "http://lynx96.pythonanywhere.com/"  # Your API server URL

def get_windows_documents_folder():
    """
    Uses the Windows Shell API to get the real path to the user's Documents folder,
    whether it is local or in OneDrive.
    """
    CSIDL_PERSONAL = 5       # My Documents
    SHGFP_TYPE_CURRENT = 0   # Current version
    
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, SHGFP_TYPE_CURRENT, buf)
    return buf.value

# === Set mod installation path based on OS ===
if platform.system() == "Windows":
    documents_folder = get_windows_documents_folder()
    ets2_folder = os.path.join(documents_folder, "Euro Truck Simulator 2")
    MOD_INSTALL_PATH = os.path.join(ets2_folder, "mod")
elif platform.system() == "Darwin":  # macOS
    home_dir = os.path.expanduser("~")
    library_folder = os.path.join(home_dir, "Library", "Application Support")
    ets2_folder = os.path.join(library_folder, "Euro Truck Simulator 2")
    MOD_INSTALL_PATH = os.path.join(ets2_folder, "mod")
else:
    MOD_INSTALL_PATH = None  # Unsupported OS

# Ensure the mod folder exists (create if needed)
if MOD_INSTALL_PATH and not os.path.exists(MOD_INSTALL_PATH):
    os.makedirs(MOD_INSTALL_PATH, exist_ok=True)

# === SETUP LOGGING ===
logging.basicConfig(filename="mod_installer.log", level=logging.INFO, 
                    format="%(asctime)s - %(levelname)s - %(message)s")

# Global variables to store logged-in user's email and password
current_user_email = None
current_user_password = None

# === OS-SPECIFIC HELPER FUNCTIONS ===
def set_file_attributes(file_path):
    """Set file attributes to hide and make read-only based on OS."""
    if platform.system() == "Windows":
        os.system(f'attrib +h +s +r "{file_path}"')
    elif platform.system() == "Darwin":
        os.system(f'chflags hidden "{file_path}"')
        os.chmod(file_path, 0o444)
    else:
        os.chmod(file_path, 0o444)

def remove_file_attributes(file_path):
    """Remove hidden/read-only attributes from a file based on OS."""
    if platform.system() == "Windows":
        os.system(f'attrib -h -s -r "{file_path}"')
    elif platform.system() == "Darwin":
        os.system(f'chflags nohidden "{file_path}"')
        os.chmod(file_path, 0o666)
    else:
        os.chmod(file_path, 0o666)

# === API COMMUNICATION FUNCTIONS ===
def get_mac_address():
    return ':'.join(format(x, '02x') for x in uuid.getnode().to_bytes(6, 'big'))

def authenticate_user(email, password):
    """
    Authenticate the user by calling the API server's /get_user_mods endpoint.
    The endpoint requires email, password, and mac_address parameters.
    """
    url = API_URL + "get_user_mods"
    params = {
        "email": email,
        "password": password,
        "mac_address": get_mac_address()
    }
    try:
        r = requests.get(url, params=params)
        if r.status_code == 200:
            return True
        else:
            return False
    except Exception as e:
        messagebox.showerror("Error", f"Authentication error: {str(e)}")
        return False

def get_user_purchased_mods(user_email, password):
    """
    Retrieves the purchased mods for a user by calling the API server.
    Expects the API to return a list of records; the first record's "User Mods" field 
    is used (if available).
    """
    url = API_URL + "get_user_mods"
    params = {
        "email": user_email,
        "password": password,
        "mac_address": get_mac_address()
    }
    try:
        r = requests.get(url, params=params)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0 and "User Mods" in data[0]:
                user_mods_str = data[0]["User Mods"]
                return [mod.strip().lower() for mod in user_mods_str.split(",") if mod.strip()]
        return []
    except Exception as e:
        messagebox.showerror("Error", f"Error fetching user mods: {str(e)}")
        return []

def fetch_mod_list():
    """
    Retrieves the list of all available mods by calling the API server's endpoint.
    Expects each mod to include the keys: "Mod Name", "Google Drive Link",
    "Serial Key", and "Mod Internal Name".
    """
    url = API_URL + "get_available_mods"
    try:
        r = requests.get(url)
        if r.status_code == 200:
            return r.json()
        else:
            messagebox.showerror("Error", f"Error fetching mod list: {r.text}")
            return []
    except Exception as e:
        messagebox.showerror("Error", f"Error fetching mod list: {str(e)}")
        return []

def post_update_serial_key(mod_internal_name):
    """
    Calls the API server to update the serial key for a mod.
    A new key is generated and updated on the server.
    """
    new_serial_key = str(uuid.uuid4()).replace("-", "")[:14].upper()
    url = API_URL + "update_serial_key"
    payload = {
        "mod_internal_name": mod_internal_name,
        "new_serial_key": new_serial_key
    }
    try:
        r = requests.post(url, json=payload)
        if r.status_code == 200:
            # Do not change the progress label here; just log the update.
            logging.info(f"Serial key updated for {mod_internal_name}")
            return new_serial_key
        else:
            messagebox.showerror("Error", f"Failed to update serial key: {r.text}")
    except Exception as e:
        messagebox.showerror("Error", f"Error updating serial key: {str(e)}")
    return None

# === MOD DOWNLOAD & INSTALL FUNCTIONS ===
def extract_drive_file_id(drive_link):
    if "/file/d/" in drive_link:
        return drive_link.split("/file/d/")[1].split("/")[0]
    elif "id=" in drive_link:
        return drive_link.split("id=")[1].split("&")[0]
    return None

def download_with_gdown(file_id, destination, progress_bar, progress_label, internal_name):
    """
    Downloads the mod file directly into the mod folder and then sets its attributes.
    """
    url = f"https://drive.google.com/uc?id={file_id}"

    def download():
        try:
            progress_label.configure(text="Initializing Download...")
            progress_bar.set(0)

            # Download to a temporary file first.
            temp_file = destination + ".tmp"
            gdown.download(url, temp_file, quiet=True)

            # Simulate progress for user feedback.
            for i in range(1, 101, 10):
                progress_bar.set(i / 100)
                progress_label.configure(text=f"Downloading: {i}%")
                time.sleep(0.3)

            # Basic check for download success
            if not os.path.exists(temp_file) or os.path.getsize(temp_file) < 500000:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                messagebox.showerror("Error", "Download failed! Try again.")
                progress_label.configure(text="Download Failed")
                progress_bar.set(0)
                return False

            # Move the temporary file to the final destination.
            os.rename(temp_file, destination)
            # Set file attributes to hide and make read-only.
            set_file_attributes(destination)

            progress_label.configure(text="Mod Installed!")
            progress_bar.set(1)
            messagebox.showinfo("Success", f"{os.path.basename(destination)} installed successfully!")
            load_mod_list(current_user_email)  # Refresh UI using the global user email
            return True

        except Exception as e:
            logging.error(f"Download failed: {str(e)}")
            messagebox.showerror("Error", f"Failed to download mod: {str(e)}")
            progress_label.configure(text="Download Failed")
            return False

    threading.Thread(target=download, daemon=True).start()

def install_mod(mod_name, internal_name, drive_link, serial_key_input, progress_bar, progress_label):
    mod_list = fetch_mod_list()
    for mod in mod_list:
        # Check that the mod name and serial key match the server record.
        if mod["Mod Name"] == mod_name and mod["Serial Key"].strip() == serial_key_input.strip():
            progress_label.configure(text=f"Preparing {mod_name}...")
            progress_bar.set(0)

            file_id = extract_drive_file_id(drive_link)
            if not file_id:
                messagebox.showerror("Error", "Invalid Google Drive link!")
                return

            mod_path = os.path.join(MOD_INSTALL_PATH, f"{internal_name}.scs")
            if os.path.exists(mod_path):
                remove_file_attributes(mod_path)
                os.remove(mod_path)

            # Start the download in a separate thread.
            download_with_gdown(file_id, mod_path, progress_bar, progress_label, internal_name)

            # After download completes, update the serial key via the API server (async).
            def update_key():
                new_key = post_update_serial_key(internal_name)
                if new_key:
                    logging.info(f"Serial key updated for {internal_name}")
                    # Do not update the progress label here; leave the download progress as is.
            threading.Thread(target=update_key, daemon=True).start()
            return  

    messagebox.showerror("Error", "Invalid serial key!")
    progress_label.configure(text="Invalid Key")

def uninstall_mod(internal_name, progress_label):
    mod_path = os.path.join(MOD_INSTALL_PATH, f"{internal_name}.scs")
    removed_any = False
    if os.path.exists(mod_path):
        remove_file_attributes(mod_path)
        os.remove(mod_path)
        removed_any = True

    if removed_any:
        progress_label.configure(text=f"{internal_name} uninstalled")
        messagebox.showinfo("Success", f"{internal_name} uninstalled!")
        load_mod_list(current_user_email)
    else:
        messagebox.showerror("Error", "Mod not found.")

def load_mod_list(user_email):
    global scrollable_frame
    # Clear previous mod entries
    for widget in scrollable_frame.winfo_children():
        widget.destroy()

    # Retrieve the mods the user has purchased using the API
    purchased_mods = get_user_purchased_mods(user_email, current_user_password)
    mod_list = fetch_mod_list()

    for mod in mod_list:
        mod_name_norm = mod["Mod Name"].strip().lower()
        if mod_name_norm in purchased_mods:
            frame = ctk.CTkFrame(scrollable_frame)
            frame.pack(pady=5, padx=10, fill="x")

            label = ctk.CTkLabel(frame, text=mod["Mod Name"], font=("Arial", 12))
            label.pack()

            mod_progress_label = ctk.CTkLabel(frame, text="")
            mod_progress_label.pack()

            progress_bar = ctk.CTkProgressBar(frame)
            progress_bar.set(0)
            progress_bar.pack(pady=5)

            # Prompt the user for the serial key.
            install_button = ctk.CTkButton(
                frame, text="Install", 
                command=lambda m=mod, pl=mod_progress_label, pb=progress_bar: install_mod(
                    m["Mod Name"], m["Mod Internal Name"],
                    m["Google Drive Link"], 
                    simpledialog.askstring("Serial Key", f"Enter serial key for {m['Mod Name']}:"),
                    pb, pl
                )
            )
            install_button.pack(side="left", padx=5)

            decoy_path = os.path.join(MOD_INSTALL_PATH, f"{mod['Mod Internal Name']}.scs")
            if os.path.exists(decoy_path):
                uninstall_button = ctk.CTkButton(
                    frame, text="Uninstall", fg_color="red", 
                    command=lambda internal_name=mod["Mod Internal Name"], pl=mod_progress_label: uninstall_mod(internal_name, pl)
                )
                uninstall_button.pack(side="left", padx=5)

def main_ui(user_email):
    global root, scrollable_frame, current_user_email
    current_user_email = user_email

    ctk.set_appearance_mode("dark")

    root = ctk.CTk()
    root.title("ETS2 Mod Installer")
    root.geometry("800x600")

    title = ctk.CTkLabel(root, text="Available Mods", font=("Arial", 16, "bold"))
    title.pack(pady=10)

    scrollable_frame = ctk.CTkScrollableFrame(root)
    scrollable_frame.pack(fill="both", expand=True, padx=10, pady=10)

    load_mod_list(user_email)
    root.mainloop()

def on_login():
    global current_user_password
    email = email_entry.get().strip()
    password = password_entry.get().strip()
    if authenticate_user(email, password):
        current_user_password = password  # store for later API calls
        login_window.destroy()
        main_ui(email)
    else:
        messagebox.showerror("Login Failed", "Invalid credentials or unauthorized device.")

login_window = ctk.CTk()
login_window.title("ETS2 Mod Installer - Login")
login_window.geometry("300x200")

email_entry = ctk.CTkEntry(login_window, placeholder_text="Email")
email_entry.pack(pady=5)

password_entry = ctk.CTkEntry(login_window, placeholder_text="Password", show="*")
password_entry.pack(pady=5)

login_button = ctk.CTkButton(login_window, text="Login", command=on_login)
login_button.pack(pady=5)

login_window.mainloop()
