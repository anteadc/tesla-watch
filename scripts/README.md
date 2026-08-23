# Tesla Model Y CPO Watch (Dubai)

Checks Tesla's inventory roughly every 30 seconds and messages you on
Telegram the moment a used Model Y shows up in the UAE. Runs free, forever,
on GitHub.

## Your two questions, answered directly

**Can strangers see my Telegram info on a public repo?**
No. The bot token and chat ID go into GitHub's "Secrets" storage, not into
any file. Secrets are encrypted, nobody can view them (not even you, after
saving), and GitHub auto-blocks them from ever appearing in logs. Public
only means people can see the *code*, never the secrets.

**Can it check every 30 seconds?**
Not with a pure GitHub schedule, GitHub refuses anything faster than every
5 minutes, no exceptions. Workaround: each 5-minute run doesn't check once
and stop, it checks every 30 seconds in a loop for the full 4.5 minutes,
then hands off to the next run. So in practice you get 30-second checks
with only a small gap between runs, usually under a minute. It's not
watch-a-clock exact, occasionally GitHub itself is slow to start a run, but
it's as close as a free setup gets. If you ever want zero gaps at all, the
only way is leaving a script running on a computer you own 24/7. Not
needed here.

---

## Setup, step by step

### Part 1: Put the files on GitHub

1. Go to **github.com**. Make a free account if you don't have one.
2. Click the **+** icon, top right corner → **New repository**.
3. Give it any name, e.g. `tesla-watch`. Leave it set to **Public**
   (that's what keeps it free and fast). Click **Create repository**.
4. You'll land on an empty repo page. Click **"Add file"** → **"Create new file."**
5. In the box that asks for a filename, type exactly:
   `scripts/tesla_check.py`
   (typing the `/` automatically creates the `scripts` folder for you)
6. Below that, paste in the whole content of the `tesla_check.py` file I
   gave you.
7. Scroll down, click the green **"Commit changes"** button.
8. Repeat steps 4–7 three more times, once for each remaining file. Use
   these exact filenames:
   - `.github/workflows/tesla-watch.yml`
   - `seen_vins.json`
   - `README.md`

You now have 4 files in your repo. That's the whole app.

### Part 2: Make the Telegram bot

1. Open Telegram. Search for **BotFather** (it has a blue checkmark).
2. Send it the message `/newbot`. Answer its two questions (a name, then
   a username ending in "bot").
3. It replies with a long token, looks like `123456789:AAFabc...`. Copy it
   somewhere safe.
4. Now search for the bot you just made (by the username you picked) and
   send it any message, like "hi". This step is required, the bot can't
   message you first.
5. Open a new browser tab and go to this address, swapping in your token:
   `https://api.telegram.org/bot123456789:AAFabc.../getUpdates`
   (replace everything after "bot" with your real token)
6. You'll see some text on the page. Look for `"chat":{"id":` followed by
   a number. That number is your chat ID, copy it too.

### Part 3: Connect the two

1. Back in your GitHub repo, click **Settings** (top menu of the repo).
2. In the left sidebar, click **Secrets and variables** → **Actions**.
3. Click **"New repository secret."**
   - Name: `TELEGRAM_BOT_TOKEN` → Value: paste your token → Save.
4. Click **"New repository secret"** again.
   - Name: `TELEGRAM_CHAT_ID` → Value: paste your chat ID → Save.

### Part 4: Turn it on

1. Click the **Actions** tab at the top of your repo.
2. Click the workflow named **"Tesla Model Y CPO Watch"** in the left list.
3. Click **"Run workflow"** (a small dropdown button) → **"Run workflow."**
4. Wait about a minute, refresh the page, click into the run that appears,
   and open the "Check inventory" step. You should see text like
   "No new listings" or a raw sample of car data.

If step 4 shows an error instead, it's almost certainly the query settings
in Part 5 below, not something you did wrong.

### Part 5: One thing to double-check

I built the search query (which cars, which country) from public examples,
but I couldn't test it against Tesla's actual UAE site myself. Do this
once to confirm it's right:

1. In Chrome, go to `tesla.com/en_AE/inventory/used/my`
2. Right-click anywhere → **Inspect** → click the **Network** tab at the top.
3. Refresh the page.
4. In the list that appears, click into anything with "inventory-results"
   in the name.
5. Find the part that says `"market"` and `"super_region"`. Compare those
   two values to what's in your `tesla_check.py` file (open it on GitHub,
   click the pencil icon to edit, they're near the top).
6. If they're different, fix them in the GitHub file, commit, then repeat
   Part 4 to re-test.

Once that test run works cleanly, you're done. It now checks by itself,
no need to open anything again unless you want to change what it's
searching for.
