# FlashShare

Lightweight self-hosted file-sharing service. Upload, process, share — done.

## Features

- 📁 Upload files up to 50MB (58 formats supported)
- 🔗 Temporary download links with expiry & download limits
- 📱 QR code generation for each shared file
- 🔧 Workspace tools: rename, delete, file-type-aware tool area
- 🌍 10-language UI: English, 中文, 日本語, 한국어, Español, العربية, Deutsch, Русский, Português, Français
- 🔄 RTL layout support for Arabic
- 👤 User accounts: local registration or X (Twitter) OAuth login
- 🔗 X account binding/unbinding for local users
- 🗄️ Gokapi as file storage backend
- 📦 Pure Python stdlib — **zero third-party dependencies** (Pillow optional)
- 🚀 Streaming upload — 64KB chunks, never loads full file into memory

## Requirements

- Python 3.12+ (3.13 compatible)
- [Gokapi](https://github.com/Forceuk/Gokapi) for file storage
- nginx (recommended) for HTTPS and reverse proxy

## Quick Start

```bash
git clone https://github.com/samelabs/FlashShare.git
cd FlashShare

# Run setup script (creates .env, data dir, initializes database)
./setup.sh

# Edit .env — fill in your X OAuth and Gokapi keys
nano .env

# Start the server
python3 main.py
```

The server listens on `127.0.0.1:53843` by default.

## Database

FlashShare uses SQLite with WAL mode. The database file is created automatically on first run via `init_db()` — no manual migration needed.

Default location: `/var/lib/flashshare/flashshare.sqlite` (configurable via `FLASHSHARE_DB`).

### Schema

| Table | Purpose |
|-------|---------|
| `users` | Local accounts + X binding info |
| `sessions` | Login session tokens (HMAC-SHA256 hashed) |
| `oauth_states` | X OAuth2 PKCE state tracking |
| `uploads` | File metadata + Gokapi references |

## Configuration

All settings via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASHSHARE_ADDR` | `127.0.0.1:53843` | Listen address |
| `FLASHSHARE_BASE_URL` | `http://localhost:53843` | Public-facing URL |
| `FLASHSHARE_DB` | `/var/lib/flashshare/flashshare.sqlite` | SQLite database path |
| `FLASHSHARE_MAX_UPLOAD_MB` | `50` | Max upload size |
| `FLASHSHARE_X_CLIENT_ID` | — | X OAuth2 client ID |
| `FLASHSHARE_X_CLIENT_SECRET` | — | X OAuth2 client secret |
| `FLASHSHARE_GOKAPI_URL` | `http://127.0.0.1:53842` | Gokapi backend URL |
| `FLASHSHARE_GOKAPI_KEY` | — | Gokapi API key |
| `FLASHSHARE_SESSION_SECRET` | — | Session signing secret (**required**) |

## Production Deployment

### systemd

```ini
# /etc/systemd/system/flashshare.service
[Unit]
Description=FlashShare file sharing service
After=network.target gokapi.service

[Service]
Type=simple
User=flashshare
WorkingDirectory=/opt/flashshare
EnvironmentFile=/opt/flashshare/.env
ExecStart=/usr/bin/python3 /opt/flashshare/main.py
Restart=always
RestartSec=3
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

### nginx reverse proxy

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    client_max_body_size 60M;

    location / {
        proxy_pass http://127.0.0.1:53843;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Project Structure

```
FlashShare/
├── main.py              # HTTP server + routing + request handlers
├── config.py            # Environment config + file type whitelist
├── database.py          # SQLite schema + init + migrations
├── auth.py              # User auth: local + X OAuth2
├── gokapi.py            # Gokapi API client (streaming upload)
├── uploads.py           # Upload quota + CRUD
├── views.py             # HTML page rendering (all pages)
├── i18n.py              # 10-language translations (106 keys each)
├── security.py          # Password hashing + file sniffing + CSRF
├── multipart.py         # Streaming multipart/form-data parser
├── tools.py             # Tool registry framework (view/peek/produce)
├── tools_registry.py    # Tool registrations
├── utils.py             # Helpers: formatting, escaping, QR, IDs
├── static/
│   ├── app.js           # Frontend logic
│   ├── style.css        # All styles
│   ├── sw.js            # Service worker (network-first)
│   ├── manifest.json    # PWA manifest
│   └── icons/           # Favicon, PWA icons, OG image
├── templates/           # Static legal pages (terms, privacy)
├── docs/                # Design docs
├── setup.sh             # First-time setup script
├── .env.example         # Environment template
└── README.md
```

## Security

- SQL injection: all queries use parameterized `?` placeholders
- XSS: all user content escaped via `html_escape`
- CSRF: all POST routes check `Origin` header
- Sessions: HttpOnly + Secure + SameSite=Lax cookies, HMAC-SHA256 token hashing
- File validation: magic number verification for images, PDF, ZIP, 7z, RAR, Office
- Path traversal: `sanitize_filename` + extension lock on rename

## License

MIT
