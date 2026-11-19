# Reel Bot – setup guide (all free)

Every 6 hours GitHub runs the bot on its own servers (your PC can be off):
find popular YouTube video -> score the best 30-60s moments -> build blurred-background reel with karaoke captions -> post to your Facebook Page.

## Step 1 – Get 4 keys

**A. YouTube API key** – console.cloud.google.com -> new project -> "APIs & Services" -> enable **YouTube Data API v3** -> Credentials -> Create API key.

**B. Gemini API key** – aistudio.google.com -> "Get API key". (If the model name in `config.json` stops working, change `gemini_model` to the current free Flash model.)

**C. Facebook Page ID + Page token**
1. developers.facebook.com -> Create App (type "Business") -> add **Facebook Login for Business** / use Graph API Explorer.
2. In Graph API Explorer pick your app, add permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `publish_video`. Generate a User token.
3. Exchange it for a long-lived user token (Access Token Tool / `oauth/access_token?grant_type=fb_exchange_token`).
4. Call `GET /me/accounts` with that long-lived token. The result lists your Page with its `id` (= FB_PAGE_ID) and `access_token` (= FB_PAGE_TOKEN). A Page token obtained this way does not expire.
5. You must be an admin of the Page and of the app. While the app is in Development mode, posting to your own Page works without app review.

## Step 2 – Put the project on GitHub
1. Create a free account at github.com, then **New repository** (public = unlimited free minutes; private = 2,000 min/month, still enough).
2. Upload ALL files of this folder, **keeping the `.github/workflows` folder** (easiest: unzip, then drag the contents into the repo page, or use GitHub Desktop).

## Step 3 – Add secrets
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Name | Value |
|---|---|
| `YT_API_KEY` | YouTube API key |
| `GEMINI_API_KEY` | Gemini key |
| `FB_PAGE_ID` | your Page ID |
| `FB_PAGE_TOKEN` | the Page access token |
| `YT_COOKIES` | *(only if the test in step 4 fails)* see below |

## Step 4 – Test in this order
1. **Actions -> "Test YouTube download" -> Run workflow.** Must go green. This checks that YouTube lets GitHub's servers download.
   If it fails with "Sign in to confirm you're not a bot": export your YouTube cookies (browser extension "Get cookies.txt LOCALLY", use a throwaway Google account, not your main), paste the whole file into the `YT_COOKIES` secret, and re-run.
2. **Actions -> "Post reel" -> Run workflow -> tick "Dry run".** It builds a reel without posting. Download `dry-run-reel` from the run's Artifacts and check how it looks.
3. Run it again with dry run **unticked** to post one real reel.
4. After that it runs by itself every 6 hours.

## Change settings
Edit `config.json` in GitHub (pencil icon):
- `topic` – what to search for
- `min_score` – virality threshold (65 default). Raise = pickier / fewer posts, lower = more posts.
- `videos_to_evaluate` – how many top videos to compare each cycle
- `days_back` – what counts as "current" popularity

## Notes
- If no clip reaches `min_score`, the bot skips that cycle instead of posting weak content.
- `used_segments.json` is the record; the bot commits it after each successful post.
- GitHub may delay scheduled runs by several minutes, and pauses schedules in repos with no activity for 60 days (the bot's own commits normally count, but check the Actions tab now and then).
- Re-posting other people's videos can cause copyright claims on Facebook. You accepted that risk; crediting is included in every caption.
