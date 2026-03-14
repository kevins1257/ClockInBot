  ⏱  ClockIn Bot — README
  Written by Kevin S


A powerful, premium-quality time-tracking bot for Discord servers.

--------------------------------------------------------------------------------
WHAT IS CLOCKIN BOT?
--------------------------------------------------------------------------------

ClockIn Bot is a Discord bot built by Kevin S that lets you track how long users
spend on different tasks (called "masks") inside your Discord server. It's
designed for teams, staff rosters, and any community that needs to log work
time — right inside Discord.

Users can clock in and out with a single button click, and admins get a full
suite of reporting and management tools.


--------------------------------------------------------------------------------
FEATURES
--------------------------------------------------------------------------------

FOR USERS:
  • Clock in and out with a single button click
  • See your current clock-in status instantly
  • Check your total accumulated time per mask
  • View session history with timestamps
  • Download your personal time data as a CSV
  • See weekly time reports with visual progress bars

FOR ADMINS:
  • Full leaderboard — see who has logged the most time
  • Force clock users in or out
  • Manually add or remove time from any user
  • Export all server data to CSV
  • Complete summary view with per-user breakdowns
  • Role assignment on clock in/out
  • Minimum role restriction for clocking in
  • Full action logging to a dedicated channel
  • Reset all data with a safety confirmation step

TECHNICAL HIGHLIGHTS:
  • Persistent SQLite database — data survives bot restarts
  • Persistent button panel — buttons still work after restart
  • Shared DB connection with WAL mode — fast, non-blocking
  • Per-guild in-memory caching — eliminates redundant DB writes
  • All responses are ephemeral by default (only visible to the user)


--------------------------------------------------------------------------------
QUICK START
--------------------------------------------------------------------------------

  1. Install Python 3.10+ and run:   pip install -r requirements.txt
  2. Open bot.py and paste your bot token at the top (YOUR_BOT_TOKEN_HERE)
  3. Run the bot:                     python bot.py
  4. In your server, use /masktoggle to enable a mask
  5. Use /settings setdefault to set a default mask
  6. Use /button in any channel to post the clock-in/out panel


--------------------------------------------------------------------------------
USER COMMANDS
--------------------------------------------------------------------------------

  /clockin [mask]              Clock in for a mask. Uses the server default
                               mask if none is specified.

  /clockout [mask]             Clock out from a mask and save the session.

  /status [@user]              See your current clock-in status and total
                               time per mask.

  /history [@user]             View your recent sessions with timestamps
                               and durations.

  /report [@user] [days]       Visual time report for the last N days with
                               progress bars.

  /leaderboard [mask] [days]   Top 10 users by total clocked time,
                               filterable by mask and date range.

  /whoclocked [mask]           See everyone currently clocked in right now.

  /export                      Download your personal session history as
                               a CSV file.


--------------------------------------------------------------------------------
ADMIN COMMANDS
--------------------------------------------------------------------------------

  /add @user mask mins         Manually add (or subtract) minutes for a
                               user on a specific mask.

  /forceout @user mask         Force a user to clock out and save their
                               current session.

  /cleardata @user [mask]      Delete all session data for a user,
                               optionally per-mask.

  /alldata [place]             Export all server sessions to CSV.
                               Send to DM or channel.

  /summary [place]             Show total time per user across all masks
                               with CSV export.

  /resetall                    Send a full summary to your DMs, then wipe
                               all session data (confirmation required).

  /masklist                    View all masks and whether they are enabled
                               or disabled.

  /masktoggle mask             Enable or disable a mask for clocking.

  /maskchange old new          Rename a mask (updates all historical data).

  /button                      Post the interactive clock in/out panel in
                               the current channel.

  /settings view               View all current bot settings for this server.

  /settings setdefault mask    Set the default mask used when none specified.

  /settings logging true/false Enable or disable action logging.

  /settings loggingchannel #ch Set which channel to send log events to.

  /settings ephemeral t/f      Toggle whether bot responses are public.

  /settings giverole true/false Enable or disable automatic role assignment.

  /settings onerole @role      Set a role to give users when they clock in.

  /settings maskrole mask @role Set a role specific to a particular mask.

  /settings minrole @role      Set a minimum role required to clock in.


--------------------------------------------------------------------------------
WHAT ARE MASKS?
--------------------------------------------------------------------------------

Masks are the categories you track time under. Think of them like job roles,
tasks, or departments. For example: "support", "development", "moderation".

By default each server starts with 12 masks named mask1 through mask12.
Use /maskchange to rename them and /masktoggle to enable the ones you want.

NOTE: Masks must be enabled before users can clock in to them.


--------------------------------------------------------------------------------
THE BUTTON PANEL
--------------------------------------------------------------------------------

Post the panel in any channel with /button. It has three buttons:

  [ clock in / out ]   Toggles your clock-in status. Uses the default mask
                       automatically, or shows a dropdown if multiple masks
                       are enabled.

  [ ? ]                Shows whether you are currently clocked in and which
                       mask(s) you are active on.

  [ 🕐 ]               Shows your total accumulated time, broken down to
                       days, hours, minutes, and seconds.


--------------------------------------------------------------------------------
FILES IN THIS PROJECT
--------------------------------------------------------------------------------

  bot.py             Main bot file — all commands and logic
  requirements.txt   Python dependencies to install with pip
  clockin.db         Auto-created SQLite database (all session data)
  README.txt         This file — written by Kevin S
  REQUIREMENTS.txt   Full dependency and setup reference



  ClockIn Bot  |  README  |  Written by Kevin Shah
