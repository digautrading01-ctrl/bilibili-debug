# bilibili-downloader-python

A web-based Bilibili video downloader built with **Python Flask** and **yt-dlp**.
Supports all available quality settings from 360p Smooth up to **4K Ultra HD (2160p)**, HDR, Dolby Vision, and 8K — depending on your Bilibili account tier.

---

## Features

- **All quality tiers** — 360p · 480p · 720p · 720p60 · 1080p · 1080p+ · 1080p60 · 4K · HDR Real · Dolby Vision · 8K
- **All Bilibili URL formats** — BV, AV, b23.tv short links, mobile (`m.bilibili.com`), multi-part (`?p=2`), and URLs with arbitrary tracking query parameters
- **Real-time progress** — download speed, bytes transferred, and ETA via Server-Sent Events
- **Audio/video merging** — separate DASH streams are automatically merged into a single `.mp4` via FFmpeg
- **Cookie support** — use an exported `cookies.txt` file or load your current local browser session automatically for access to higher-quality streams

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10 or newer |
| FFmpeg | any recent release |

### Install FFmpeg

**Windows**
```
winget install ffmpeg
```
or download from <https://ffmpeg.org/download.html> and add the `bin/` folder to `PATH`.

**macOS**
```
brew install ffmpeg
```

**Ubuntu / Debian**
```
sudo apt install ffmpeg
```

Verify installation:
```
ffmpeg -version
```

---

## Installation

```bash
git clone https://github.com/your-username/bilibili-downloader-python.git
cd bilibili-downloader-python
pip install -r requirements.txt
```

---

## Running the App

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

The server listens on `0.0.0.0:5000` by default, so it is reachable from other devices on the same local network.

---

## Usage

1. Paste any Bilibili video URL into the input field and press **Fetch** (or hit Enter).
2. The app retrieves the video title, thumbnail, uploader, and all available quality options.
3. Select the desired quality from the grid.
4. Click **Download** and watch the real-time progress bar.
5. When the download is complete, click **Save File** to save it to your computer.
6. Click **New Download** to reset and download another video (the previous file is deleted from the server).

---

## Supported URL Formats

All of the following are handled correctly — query parameters such as `vd_source`, `spm_id_from`, `from_search`, and `t` (timestamp) are stripped automatically; only `p` (playlist part) is kept.

| Example | Notes |
|---|---|
| `https://www.bilibili.com/video/BVxxxxxxxxxx` | Standard BV link |
| `https://www.bilibili.com/video/avxxxxxxxxxx` | Legacy AV link |
| `https://b23.tv/xxxxxxx` | Short link |
| `https://m.bilibili.com/video/BVxxxxxxxxxx` | Mobile link |
| `https://bilibili.com/video/BVxxxxxxxxxx` | Without www |
| `https://www.bilibili.com/video/BVxxxxxxxxxx?p=3` | Multi-part video, part 3 |
| `https://www.bilibili.com/video/BVxxxxxxxxxx?p=2&vd_source=abc123&t=42` | Tracking params stripped |

---

## Quality Tiers

| Quality ID | Label | Notes |
|---|---|---|
| 127 | 8K Ultra HD (4320p) | Requires Bilibili Premium |
| 126 | Dolby Vision (2160p) | Requires Bilibili Premium |
| 125 | HDR Real (2160p) | Requires Bilibili Premium |
| 120 | 4K Ultra HD (2160p) | Requires Bilibili Premium |
| 116 | 1080P60 High Frame Rate | Requires login + Premium |
| 112 | 1080P+ High Bandwidth | Requires login + Premium |
| 80  | 1080P High Definition | Requires login (free) |
| 74  | 720P60 High Frame Rate | Requires login (free) |
| 64  | 720P High Definition | Free, no login required |
| 32  | 480P Clear | Free, no login required |
| 16  | 360P Smooth | Free, no login required |

Only qualities that are actually available for the given video are shown in the UI.

---

## Using Cookies for Higher Quality

To download 1080p and above you need to be logged in to Bilibili.
The app supports two authentication sources, in this order:

1. `cookies.txt` in the project root
2. Your current local browser profile on the same machine

If `cookies.txt` exists, it is used first. If it does not exist, the app tries to read Bilibili cookies directly from a supported local browser profile through `yt-dlp`.

### Option 1: exported `cookies.txt`

Export your browser cookies in **Netscape / cookies.txt** format and save the file as `cookies.txt` in the project root (next to `app.py`).

### Recommended browser extension

- Chrome / Edge: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
- Firefox: [cookies.txt](https://addons.mozilla.org/firefox/addon/cookies-txt/)

1. Log in to bilibili.com in your browser.
2. Export cookies for `bilibili.com` using the extension.
3. Save the exported file as `cookies.txt` in the project root.
4. Restart the Flask server — the app will detect and use the file automatically.

### Option 2: current browser session

If no `cookies.txt` file is present, the app will try to load cookies for `bilibili.com` from the current local browser profile.

- Supported browsers: Safari, Edge, Chrome, Firefox, Brave, Opera, and Vivaldi
- This only uses cookies already available in your own local browser profile
- The UI shows which auth source is active: `cookies.txt`, browser cookies, or guest access

### Safari on macOS

Safari is supported, but macOS privacy controls may block Python from reading Safari's cookie store.

If the UI says Safari cookies could not be read:

1. Grant **Full Disk Access** to the app that is running Python / Flask
2. Restart the app and try **Fetch** again
3. If Safari access is still blocked, export Bilibili cookies to `cookies.txt` and place it in the project root

If browser-cookie loading does not work on your machine, use the exported `cookies.txt` method above.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Serves the web UI |
| `POST` | `/api/info` | Fetch video metadata and available formats |
| `POST` | `/api/download` | Start a background download, returns `task_id` |
| `GET` | `/api/progress/<task_id>` | Server-Sent Events stream for real-time progress |
| `GET` | `/api/file/<task_id>` | Download the completed `.mp4` file |
| `DELETE` | `/api/cleanup/<task_id>` | Remove the task and its files from the server |

### POST `/api/info`

**Request body**
```json
{ "url": "https://www.bilibili.com/video/BVxxxxxxxxxx" }
```

**Response**
```json
{
  "title": "Video Title",
  "thumbnail": "https://...",
  "duration": 312,
  "uploader": "UploadUsername",
  "view_count": 1234567,
  "url": "https://www.bilibili.com/video/BVxxxxxxxxxx",
  "auth": {
    "mode": "browser",
    "has_auth": true,
    "source": "Safari cookies",
    "level": "info",
    "message": "Using local Safari cookies for authenticated access."
  },
  "formats": [
    {
      "id": "80",
      "label": "1080P High Definition",
      "height": 1080,
      "fps": 30,
      "vcodec": "avc1.640028",
      "acodec": "none",
      "ext": "m4s",
      "filesize": null
    }
  ]
}
```

### POST `/api/download`

**Request body**
```json
{ "url": "https://www.bilibili.com/video/BVxxxxxxxxxx", "format_id": "80" }
```

**Response**
```json
{ "task_id": "uuid-string" }
```

### GET `/api/progress/<task_id>` — SSE stream

Each message is a JSON object:

| Field | Type | Description |
|---|---|---|
| `status` | string | `pending` / `downloading` / `processing` / `done` / `error` |
| `percent` | float | 0–100 |
| `speed` | float \| null | Bytes per second |
| `eta` | int \| null | Estimated seconds remaining |
| `downloaded` | int | Bytes downloaded so far |
| `total` | int | Total bytes (0 if unknown) |
| `filename` | string \| null | Output filename (set when `status` is `done`) |
| `error` | string \| null | Error message (set when `status` is `error`) |

---

## Project Structure

```
bilibili-downloader-python/
├── app.py              # Flask application
├── requirements.txt    # Python dependencies
├── cookies.txt         # (optional) Bilibili session cookies
├── templates/
│   └── index.html      # Single-page web UI
├── downloads/          # Created at runtime; stores downloaded files
└── README.md
```

---

## Notes

- Downloaded files are stored in the `downloads/` directory under a per-task UUID sub-folder. They are deleted automatically when you click **New Download** in the UI, or when you call `DELETE /api/cleanup/<task_id>`.
- The server does **not** cache or re-serve files across restarts. Task state is in-memory only.
- High-quality streams (1080p and above) on Bilibili are distributed as separate video and audio DASH streams; FFmpeg is required to merge them into a single file.
- The app uses standard authenticated access via your own cookies. It does not include stealth or anti-bot bypass features.
- On macOS, Safari cookie access can fail if the Python host process does not have permission to read Safari's cookie storage.
- This tool is intended for **personal, offline use** of content you are entitled to access. Always respect Bilibili's Terms of Service and the rights of content creators.
