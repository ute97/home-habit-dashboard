# Home Habit Dashboard

This app runs inside Home Assistant and stores its SQLite database in the persistent `/data` directory. Its UI is exposed through Home Assistant ingress; it does not need an exposed host port or a Home Assistant long-lived access token.

## Configuration

Set the app's `timezone` option to an IANA timezone name, for example `Europe/Amsterdam`. This is used to decide when a scheduled habit period has ended and when a missed period can consume a freeze token.

## Data and backups

The SQLite database is stored at `/data/habits.sqlite3`. Home Assistant includes the app's persistent data in app backups. Use **Export data** from the profile menu to download a JSON backup. **Import data** replaces the app's current profiles and tracker data; export first if you may need to undo an import.

The tracker does not create Home Assistant entities or call Home Assistant services. The shared profile picker is not an authentication boundary; people with access to this dashboard can see and edit every profile.

## Freeze tokens

Missed daily or selected-weekday occurrences use one token per missed occurrence. Weekly-target and monthly-target schedules use one token when the period ends below its target. A missed occurrence/period is checked once, even if the profile has no tokens at that time; a token granted later does not retroactively protect it. Remaining misses still break the streak. Grant tokens manually from the profile menu.
