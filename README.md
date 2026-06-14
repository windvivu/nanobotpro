# Nanobot Pro

Nanobot Pro la ban runtime toi gian cua local Nanobot. Ban nay giu cac thanh phan chinh de chay bot, dashboard va Adminbot, nhung khong kem docs, tests hay Docker setup.

## Chuc Nang

- Chay Nanobot gateway voi dashboard FastAPI.
- Quan ly cau hinh, provider, model preset, credential profile, OAuth va MCP tu dashboard.
- Ho tro Adminbot de tao va quan ly nhieu bot tren cung mot may.
- Ho tro cac kenh va cong cu runtime co trong package `nanobot/`.
- Giu WhatsApp bridge Node.js trong `bridge/` neu can dung WhatsApp channel.

## Phien Ban

- Local runtime version: `0.2.1`
- Python: `>=3.11`

## Cach Chay

### Cai Dat

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[web]"
```

Ubuntu/macOS:

```bash
python -m venv venv
source ./venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[web]"
```

### Chay Mot Bot

Windows:

```powershell
.\venv\Scripts\python.exe -m nanobot.cli.commands gateway --web
```

Ubuntu/macOS:

```bash
./venv/bin/python -m nanobot.cli.commands gateway --web
```

Mo dashboard:

```text
http://127.0.0.1:8899
```

### Chay Adminbot Quan Ly Nhieu Bot

Windows:

```powershell
.\nanobot-launcher.cmd
```

Hoac:

```powershell
.\venv\Scripts\python.exe -m adminbot.app.main web --port 8900
```

Ubuntu/macOS:

```bash
chmod +x ./nanobot-launcher.sh
./nanobot-launcher.sh 8900
```

Mo Adminbot:

```text
http://127.0.0.1:8900
```

Dang nhap lan dau:

```text
password: abc123
```

Sau khi dang nhap, Adminbot bat buoc doi mat khau truoc khi tao hoac chay bot.

### Remote Bind

Chi bind ra mang ngoai khi da doi mat khau va co lop bao ve phu hop:

```powershell
.\nanobot-launcher.ps1 -Port 8900 -HostName 0.0.0.0
```

```bash
./nanobot-launcher.sh 8900 0.0.0.0
```
