# TODO (from the phone now, or when home) — 2026-11-09

## Instagram auto-publish: from your phone, ~10 min

### Option 1: Facebook Login from the phone (Tailscale)
1. developers.facebook.com → My Apps → **Figuring Clips Publisher** → Facebook Login for Business → **Settings** →
   *Valid OAuth Redirect URIs*: add exactly this one (keep the localhost one too), then **Save changes**:

   `https://leaderhub-ad8.tail8f9785.ts.net/api/instagram/oauth/callback`

   If Meta refuses it, use Option 2. Don't turn off Strict Mode.
2. Same app → Facebook Login for Business → **Configurations** → open the one whose ID is in ClipForge → Settings →
   Instagram (Login configuration ID):
   - *Access token type* must be **User access token** (not System-user).
   - *Permissions* must include instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement and
     business_management.
   - If it's wrong, create a new configuration (User access token, those permissions) and paste its ID in Settings →
     Instagram.
3. Phone (Tailscale on) → https://leaderhub-ad8.tail8f9785.ts.net → Accounts → Instagram → **Add Instagram account**.
   - On Meta's screens: *Edit previous settings* → tick the Page linked to figuringclips.in AND the Instagram account.
   - Allow **every** permission: with a configuration, refusing one means Meta grants none.
4. The card should say "Auto-publish ready". If a step fails, Accounts → Instagram shows Meta's exact error (also
   stored in `workspace/accounts/instagram/oauth_attempts.json`).

### Option 2: Connect with a token (always works remotely)
1. Phone → https://developers.facebook.com/tools/explorer
   - Top right *Meta App* = **Figuring Clips Publisher**; *User or Page* = **Get User Access Token**.
2. Add permissions: instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement,
   business_management (optional: instagram_manage_insights) → **Generate Access Token** → pick the Page +
   figuringclips.in.
3. Copy the token → ClipForge → Accounts → Instagram → **Connect with a token** → paste → Connect.
   - The laptop exchanges it for a 60-day login that renews itself; the app secret never leaves the laptop.
   - The existing account is converted in place: its queued Reels and history stay.
4. If Explorer doesn't offer a permission, add it to the app first: App dashboard → Use cases → (Instagram / Pages)
   → Customize → Add.

Prerequisites, if Meta says "no Pages with an Instagram account":
- Instagram → Settings → Account type = **Professional**.
- Facebook Page → Settings → Linked accounts → Instagram = figuringclips.in.

After connecting: `clipforge ig-test ig:manual:figuringclips.in` (or ask Claude). It uploads one container and waits for
FINISHED **without publishing** (publishing only with `instagram.test_publish_ok: true`).

## When home
- [ ] Windows auto-logon (or keep the session logged in): the new `ClipForge Service` task starts at **logon**. After
      a Windows Update reboot nothing runs until someone logs in. Tailscale keeps working, since it is a system
      service. Not changed remotely: it is a user-account setting.
- [ ] Close the old console window started by `start-ui.bat` (its server was stopped during the restart).
- [ ] `D:\clipforge` had uncommitted local edits before this fix: `.claude/launch.json`, `clipforge/doctor.py`
      (left as they were), and `config.yaml` (`cooldown_hours: 24.0`, `day_cutoff: 05:00`, written by the Settings
      page; same values as committed). config.yaml was backed up to `workspace/backups/config.yaml.pre-remote-fix`.
- [ ] YouTube Studio checks the API can't do (REACH_REPORT.md → "Where to look"): "Viewed vs swiped away", the
      Restrictions column, and who else posted the same moments.
- [ ] Decide on volume: the channel posts 5+ Shorts/day and the feed barely tests them. Not changed automatically
      (not enough data yet); the weekly reach report on Telegram will show the trend.
