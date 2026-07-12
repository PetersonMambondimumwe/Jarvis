import os
from pathlib import Path

def diag():
    print(f"Current Working Directory: {os.getcwd()}")
    print(f"Path.home(): {Path.home()}")
    print(f"Desktop Path (home / Desktop): {Path.home() / 'Desktop'}")
    
    # Check common Windows desktop locations
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        print(f"USERPROFILE: {user_profile}")
        print(f"USERPROFILE / Desktop: {Path(user_profile) / 'Desktop'}")
        
    # Check OneDrive Desktop
    onedrive = os.environ.get("ONEDRIVE")
    if onedrive:
        print(f"ONEDRIVE: {onedrive}")
        print(f"ONEDRIVE / Desktop: {Path(onedrive) / 'Desktop'}")

if __name__ == "__main__":
    diag()
