# ORB Desk

An opening range breakout (ORB) bot and planner for US stocks, running free on GitHub.

- **The bot** (`bot/orb_bot.py`) runs on GitHub Actions every 5 minutes on weekdays. During the NYSE session it builds each watchlist stock's opening range (the 9:30, 9:35 and 9:40 ET five-minute bars) and sends **one phone alert** per stock per day when a 5-minute bar closes above the range high. Each alert includes entry, stop (range midpoint), target (1R) and shares sized to your max loss. It also reminds you to be flat by the close.
- **The app** (`docs/index.html`) is a web page on GitHub Pages: the session clock, what the bot sees right now, the alert history, a planner that sizes a trade and checks it against your rules, and a daily trade book.
- **Your rules** live in `docs/config.json`. Both the bot and the app read them.

It never places orders and never tells you what to buy. An alert means "matched the ORB rule". Most people who day trade lose money. This is a planning and education tool, not financial advice.

## Setup (about 10 minutes, all in the browser)

1. **Create the repository.** On github.com click **+** → **New repository**. Name it `orb-desk`, choose **Public**, click **Create repository**.
2. **Upload the files.** On the new repository's page click **uploading an existing file**. Drag in everything from this folder (the `bot`, `docs` and `.github` folders and this README) and click **Commit changes**.
   - The `.github` folder is hidden on Mac and Windows. If it doesn't upload, create the file by hand: **Add file** → **Create new file**, name it `.github/workflows/orb-bot.yml`, and paste in the contents of that file.
3. **Phone alerts.** Install the free **ntfy** app on your phone and subscribe to a private topic name only you know, for example `orb-` followed by 16 random letters and digits. Then in the repository: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Name it `NTFY_TOPIC` and paste the topic name as the value.
4. **Turn on the app.** **Settings** → **Pages** → under *Branch* choose `main` and the `/docs` folder → **Save**. After a minute your app is at `https://YOUR-GITHUB-NAME.github.io/orb-desk/`.
5. **Test the bot.** **Actions** tab → **ORB bot** → **Run workflow**. Outside market hours it just says it is outside the scan window; that means it works. If GitHub asks you to enable workflows, click the green button.

## Good to know

- **Timing.** GitHub starts scheduled runs when it has capacity, often a few minutes late and sometimes later. Always check the breakout time and how far the price has moved before you act.
- **Data.** Prices come from Yahoo's free public chart data, which is unofficial and can lag. If Yahoo refuses GitHub's servers, the app shows "No data" for those tickers.
- **Free.** Public repositories get unlimited Actions minutes and free Pages. The repository, including the alert history, is public, so anyone with the link can see your watchlist and alerts. Your ntfy topic stays secret.
- **GitHub pauses scheduled workflows after 60 days with no activity in the repository.** Changing any file (for example your watchlist) counts as activity.
- **Holidays.** The NYSE calendar is filled in for 2026 and 2027. Add 2028 dates in `bot/orb_bot.py` and `docs/index.html` when the NYSE publishes them.
- **Test locally** (optional): `python bot/orb_bot.py --last` replays the last finished session and prints what it would have alerted.
