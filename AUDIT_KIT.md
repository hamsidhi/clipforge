# YouTube API Audit Kit (ClipForge)

Everything needed to send Google's **YouTube API Services: Audit and Quota Extension Form**:
https://support.google.com/youtube/contact/yt_api_form (checked 2026-11-04: this is the current form).

## Read this first: do you need it?
- **Not for publishing any more.** On **2 Nov 2026** Google changed the rule (YouTube Data API revision history):
  videos uploaded from **unverified/unaudited** API projects are **no longer locked to private**. ClipForge's API mode
  (private + `publishAt`, YouTube publishes at the slot) therefore works without the audit. ClipForge still checks
  every upload: if YouTube ever locks one again, the clip shows **Locked private**, with a fix.
- **What the audit gives you:** more quota than the default (10,000 units/day + 100 `videos.insert` calls/day), and an
  "audited, compliant" record for the project. At 3–6 Shorts per day you will not need more quota.
- **Honest expectations:** approval is **not guaranteed** for a personal tool that isn't publicly accessible. Reviews
  take weeks. Until then (and if it is rejected), **assisted mode** always works: upload the weekly batch in YouTube Studio
  (≈10 minutes a week), then click Sync. **API mode also works today without the audit.**

## Public pages (GitHub Pages, free)
| Page | URL |
|---|---|
| Home (primary website) | https://hamsidhi.github.io/clipforge/ |
| Privacy Policy | https://hamsidhi.github.io/clipforge/privacy.html |
| Terms of Service | https://hamsidhi.github.io/clipforge/terms.html |

The source is `docs-site/`. It is published by `.github/workflows/pages.yml` on every push to `main`.
**One-time step:** GitHub repo → Settings → Pages → *Source: GitHub Actions*. Then run the workflow (Actions → *Publish
docs-site* → Run).

The privacy policy names **YouTube API Services**, links the Google Privacy Policy and the YouTube Terms of Service,
lists every piece of data accessed (with the scope that grants it) and says the data is stored only on your computer.
It also explains how to revoke access (in the app and in Google security settings) and how data is deleted.

## Form answers (field by field)
Section 1: Request type
- **Reason:** *Compliance audit for additional quota*. Choose it even if you only want the audit stamp; you can request "no change" in quota in section 5.

Section 2: Organization & contact
- **Applying as:** Individual developer.
- **Full legal name:** `<your full name>`
- **Organization legal name:** `<your full name>` (individual).
- **Primary website:** `https://hamsidhi.github.io/clipforge/`
- **Address:** `<your address in India>`
- **Category:** Other: *Personal content-creation tool* (or "Content creator tools" if offered).
- **Organization size:** 1 (individual).
- **Primary contact:** `<your name>`, `<your Google account email>`. Technical and business contact: same as primary.

Section 3: Business model
- **Description of your work related to YouTube (100–5,000 chars):**
  > ClipForge is a free, open-source desktop application I wrote and run only on my own computer. It turns long podcast
  > episodes into short vertical clips (YouTube Shorts) with captions. I review every clip, title, description and
  > publishing time myself. ClipForge then uses the YouTube Data API to upload the approved Shorts to YouTube channels that
  > I own, as private videos with a scheduled publish time (status.publishAt), so YouTube publishes them at the chosen time.
  > It reads my own channel's uploads list and video status (videos.list, playlistItems.list, channels.list) to confirm
  > that each upload processed and was published. It uses the YouTube Analytics API, read-only and for my own channel, to
  > show views per Short on a local dashboard. There is no server, no other users and no advertising. All tokens and data
  > stay on my computer.
- **Target audience:** Other: *myself (personal use)*.
- **Monetization:** None (the tool is free and has no ads). My channel itself may earn through the YouTube Partner Program.
- **Partner Manager:** No.
- **How you learned about the API:** YouTube developer documentation.
- **Channel URL(s):** `https://www.youtube.com/@figuringclips-in` (add any other channel you connect).

Section 4: API client
- **API client name:** `ClipForge` (it must NOT contain "YouTube").
- **Contains "YouTube":** No.
- **Primary access URL:** `https://hamsidhi.github.io/clipforge/` (the app itself runs locally at `http://localhost:8000`).
- **Privacy policy URL:** `https://hamsidhi.github.io/clipforge/privacy.html`
- **Terms of service URL:** `https://hamsidhi.github.io/clipforge/terms.html`
- **Publicly accessible:** No. Personal/internal tool, installed from source, used only by me.
- **Demo account:** Not applicable. Explain that the app is a local desktop tool and point to the screencast
  (`docs/audit/clipforge-upload-flow.mp4`, upload it to Drive/YouTube as *unlisted* and paste the link) and the source code.
  Don't share passwords.
- **Special instructions:** "Local desktop app: see the screencast and the screenshots in the attachments; source code
  at https://github.com/hamsidhi/clipforge."

Section 5: Use case & quota (one project)
- **Google Cloud project number:** `<PROJECT_NUMBER>`. Find it in Cloud console → Dashboard → *Project info → Project number*. It's numeric, not the project ID.
- **Use case categories:** Video uploading; Internal tool / analytics for my own channel.
- **Users sign in with Google (OAuth 2.0):** Yes.
- **Derived metrics / 36-month storage acknowledgment:** tick it. ClipForge stores views locally and refreshes them.
  Disconnect deletes the stored statistics.
- **Expected volume:** fewer than 1,000 requests per day.
- **Endpoints used:**
  - `videos.insert`, `videos.list`, `channels.list`, `playlistItems.list`
  - YouTube Analytics `reports.query`
- **Scopes used and why:**
  | Scope | Why |
  |---|---|
  | `https://www.googleapis.com/auth/youtube.upload` | Upload the Shorts I approved (private + publishAt). |
  | `https://www.googleapis.com/auth/youtube.readonly` | Read my channel name and uploads list, video status/processing and public stats. Also link videos I uploaded myself in Studio. |
  | `https://www.googleapis.com/auth/yt-analytics.readonly` | Views per Short, and Shorts views over 90 days for my own dashboard. |
- **Quota requested:** *No change (default)*, or, if you want headroom: 20,000 units/day. Justification:
  "≤6 uploads/day (videos.insert bucket), ~1,500 units/day for status checks (videos.list every 10 min for scheduled
  videos) and daily stats. The default is enough; a small increase protects against retries."

## Attachments (all in `docs/audit/`)
| Required by the form | File |
|---|---|
| Privacy policy screenshot (YouTube section, Google Privacy Policy link, deletion) | `docs/audit/01-privacy-policy.png` |
| Homepage screenshot (privacy link + YouTube mention visible) | `docs/audit/02-homepage.png` |
| Terms of service | `docs/audit/03-terms.png` |
| OAuth flow: consent / scopes / revocation | `docs/audit/04-accounts-connect.png`, `docs/audit/08-disconnect-revoke.png`, plus your own screenshot of Google's consent screen (taken when you click *Add channel*) |
| Upload interface (title 100 chars / description 5,000 / privacy selector / channel / ToS link) | `docs/audit/05-upload-dialog.png`, `docs/audit/06-privacy-selector.png` |
| Dashboard / internal tool | `docs/audit/07-schedule.png`, `docs/audit/09-dashboard-health.png` |
| End-to-end screencast (required for upload tools) | `docs/audit/clipforge-upload-flow.mp4`. It is made with demo data, **so also record a 1-minute screen capture of a real upload through to the live Short**: Windows *Snipping Tool → Record*. |

The screenshots and screencast are regenerated with demo data (never real tokens) by:
```bash
python scripts/demo_publish.py
# then, in another terminal (port 8010, no background YouTube calls):
set CLIPFORGE_CONFIG=workspace_demo/config.yaml && set CLIPFORGE_NO_TICKER=1 && clipforge ui --port 8010 --no-browser
node scripts/audit_capture.mjs http://127.0.0.1:8010
```

## Required Minimum Functionality: how ClipForge complies (checked 2026-11-04)
| Requirement (YouTube API Developer Policies / RMF) | ClipForge |
|---|---|
| User sets the **title** (max 100 characters) | Editable in the clip dialog, counter n/100 (warns above 70). The server refuses >100. |
| User sets the **description** (max 5,000) | Editable, counter n/5000; the server refuses >5,000. |
| User chooses **public, private or unlisted** | Visibility selector: Scheduled (private → public at the slot), Public at the slot, Unlisted, Private. |
| Uploading to the right channel | The dialog shows the channel avatar + name. A channel must be **confirmed** before its first upload. |
| Link to the YouTube Terms of Service | Shown under the upload form, on Accounts, and on the public site. |
| Revoke access | Accounts → **Disconnect**: revokes the token at Google, deletes the saved login and stored stats. Google security settings link is in the privacy policy. |
| Nothing is published without the user | Every clip is approved, and every slot is chosen or confirmed, by the user. Uploads default to *private + publishAt*. |

## After you submit
Settings → Publishing → *Apply for the YouTube API audit* → set **Applied** with the date. When Google answers, set
**Approved** (this also sets `publishing.api_audited: true`) or **Rejected** (assisted mode keeps working; API mode works
too since the 2 Nov 2026 change).
