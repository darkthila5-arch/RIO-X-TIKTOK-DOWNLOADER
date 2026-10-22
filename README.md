# TikTok Downloader × Rio

Flask app: paste a TikTok link, then save the video (with or without the Rio ghost watermark),
an MP3, the original file, the cover image or a details TXT.

## Folder layout (Flask needs exactly this)

```
app.py
requirements.txt
render.yaml
templates/index.html
static/app.js
static/style.css
static/watermark.png     <- ghost logo burned into videos
static/ghost.png         <- logo in the header / hero
static/favicon.png
```

## Run locally

```bash
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

## Deploy on Render

Push this folder to GitHub, create a **Web Service** from the repo (render.yaml is picked up).

## Downloads, in short

| Tab | What you get | How |
|---|---|---|
| Video | MP4 in the chosen quality. Watermark switch + corner picker. | Watermark on: ffmpeg job. Off: direct stream. |
| Audio | Real **MP3** with caption/creator tags. | ffmpeg job. Uses TikTok's sound file; if that fails, takes the sound from the video. |
| Document | **Original video file** (no watermark, no re-encode, served as a plain file), **cover image**, **details TXT**. | Direct stream. |

Every download starts with `POST /api/prepare`, so any failure shows as a message in the page.

## Environment variables (all optional)

| Name | Default | Meaning |
|---|---|---|
| `PROVIDERS` | `own,tikwm,ytdlp` | Order of lookup providers. `own,ytdlp` never uses the third-party tikwm API. |
| `WATERMARK` | `1` | `0` switches the watermark feature off. |
| `WATERMARK_POSITION` | `br` | Default corner: `br`, `bl`, `tr`, `tl`. |
| `WATERMARK_SIZE` | `0.30` | Logo width as a share of the video width. |
| `WATERMARK_OPACITY` | `0.92` | 0.1 to 1. |
| `WATERMARK_IMAGE` | `static/watermark.png` | Use another PNG (transparent background). |
| `RATE_LIMIT` / `RATE_WINDOW` | `12` / `60` | Requests per visitor per window (seconds). |
| `GLOBAL_RATE_LIMIT` | `240` | Requests per window for the whole site. |
| `PROXY_HOPS` | `2` | See below. |
| `MAX_SOURCE_MB` | `80` | Largest video that ffmpeg will process. |
| `MAX_ACTIVE_JOBS` | `5` | Watermark/MP3 jobs allowed at once. |
| `FFMPEG_PRESET` | `ultrafast` | `veryfast` gives smaller files but uses more CPU. |
| `PROXY_URL` | - | Proxy for TikTok requests. |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | - | Keeps the download counter across restarts. |
| `DEBUG_ERRORS` | - | `1` shows the real error in the UI and enables `/api/debug/ip`. |

### Rate limit and `PROXY_HOPS`

`X-Forwarded-For` can be forged by visitors, so the app trusts only the entry added by your own
proxies, counted from the right. On Render this is normally 2. To check: set `DEBUG_ERRORS=1`,
open `/api/debug/ip` and confirm `seen_as` is **your** IP, then remove `DEBUG_ERRORS`.
If it shows a Render/Cloudflare address, change `PROXY_HOPS`.

## Notes

* Render's free plan sleeps after 15 min idle (first request is slow) and wipes temp files on restart.
* The watermark text in the old version is gone: the ghost PNG is the watermark. Replace
  `static/watermark.png` with any transparent PNG to rebrand.
* Only save videos you made or have permission to use.
