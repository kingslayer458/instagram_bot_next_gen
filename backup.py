import shutil
import os
import time

SOURCE = "/app/data"
DEST = "/app/data_backup/latest"

# Ensure backup base exists
os.makedirs("/app/data_backup", exist_ok=True)

print("Instagram backup service is running", flush=True)
print(f"Source: {SOURCE}", flush=True)
print(f"Destination: {DEST}", flush=True)
while True:
    print(f"Updating backup at {DEST}...", flush=True)

    # Remove old backup
    if os.path.exists(DEST):
        shutil.rmtree(DEST)

    # Copy fresh data
    shutil.copytree(SOURCE, DEST)

    print("Backup updated successfully. Sleeping for 12 hours...\n", flush=True)

    time.sleep(43200)  # 12 hours
