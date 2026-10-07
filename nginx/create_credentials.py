"""Tạo mật khẩu demo ngẫu nhiên, chỉ lưu local; không in secret ra log."""
import os
from pathlib import Path
import secrets
import subprocess

auth_dir = Path(__file__).resolve().parent / "auth"
auth_dir.mkdir(mode=0o700, exist_ok=True)
if (auth_dir / ".htpasswd").exists():
    raise SystemExit("Credentials đã tồn tại; giữ nguyên, không ghi đè.")
password = secrets.token_urlsafe(24)
hashed = subprocess.run(["openssl", "passwd", "-apr1", "-stdin"],
                        input=password + "\n", text=True, capture_output=True, check=True).stdout.strip()
# Nginx worker cần đọc hash, thư mục này chỉ mount read-only vào Nginx.
with (auth_dir / ".htpasswd").open("x") as handle:
    handle.write("demo:" + hashed + "\n")
os.chmod(auth_dir / ".htpasswd", 0o644)
with (auth_dir / "credentials.txt").open("x") as handle:
    handle.write("Username: demo\nPassword: " + password + "\n")
os.chmod(auth_dir / "credentials.txt", 0o600)
print("Credentials được lưu tại nginx/auth/credentials.txt (không commit).")
