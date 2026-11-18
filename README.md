# gtaclip-cloud

This runs free on GitHub Actions every 30 minutes, so no PC or OpusClip is needed. Each run:

1. Finds the newest GTA 6 Twitch clips from the approved streamers listed in `streamers.yaml`.
2. Downloads them clean, with no watermark and no captions.
3. Uploads them to Cloudinary with the tag `gtaclip_new`.

The scheduled Claude posting runs then pick the best clip, write the hook, render the boat-style 9:16 video in Cloudinary, and post it to TikTok and YouTube Shorts.

## One-time setup: add 3 secrets

In this repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add these three:

| Name | Where to get it |
|---|---|
| `TWITCH_CLIENT_ID` | https://dev.twitch.tv/console/apps → **Register Your Application** (Name `gtaclip`, OAuth Redirect `http://localhost`, Category Other, Client Type Confidential) → **Manage** → Client ID |
| `TWITCH_CLIENT_SECRET` | Same page → **New Secret** → copy it |
| `CLOUDINARY_URL` | Cloudinary Console → **Settings → API Keys** (or the Dashboard). Copy the **API environment variable**, which looks like `cloudinary://123:abc@ws7344kt`. Paste only the part that starts with `cloudinary://`. |

Then go to **Actions → "fetch fresh GTA 6 clips" → Run workflow** to test it.

## Changing streamers

Edit `streamers.yaml`. `facecam` is where the webcam sits on their stream, as fractions `[x, y, width, height]`, for example Clix `[0, 0, 0.248, 0.248]`.
