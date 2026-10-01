# Home Habit Dashboard

This app runs inside Home Assistant and stores its SQLite database in the persistent `/data` directory. Its UI is exposed through Home Assistant ingress; it does not need an exposed host port or a Home Assistant long-lived access token.

## Configuration

Set the app's `timezone` option to an IANA timezone name, for example `Europe/Amsterdam`. This is used to decide when a scheduled habit period has ended and when a missed period can consume a freeze token.

## Data and backups

The SQLite database is stored at `/data/habits.sqlite3`. Home Assistant includes the app's persistent data in app backups. Use **Export data** from the profile menu to download a JSON backup. **Import data** replaces the app's current profiles and tracker data; export first if you may need to undo an import.

The tracker does not create Home Assistant entities or call Home Assistant services. The shared profile picker is not an authentication boundary; people with access to this dashboard can see and edit every profile.

## Shared tasks

Tasks are personal by default. Mark a task **Shared with all profiles** to show the same task in every profile's list. Any dashboard user can edit, complete, reopen, or delete it; completing it updates the shared task for everyone. Shared tasks remain owned by the profile that created them, so removing that profile also removes its shared tasks. Profiles are a convenience selector, not an access-control boundary.

## Vacations

Use **Manage vacations** in the profile menu to add an inclusive date range for the selected profile. It covers all that profile's habits. Ranges may overlap; their covered dates combine. A range must start today or later. Once its start date has passed, it cannot be edited or deleted, and settled history is not recalculated.

Missed daily, selected-weekday, or selected-month-date occurrences inside a vacation are excused without using a freeze token. A weekly-target or monthly-target period is excused only when the vacation ranges cover every calendar day of the ISO week or month. Partial coverage does not lower the target and follows normal freeze-token rules. Because a vacation cannot start in the past, it cannot retroactively protect an already-settled target period.

## Freeze tokens

Grant tokens manually from the profile menu; they are spent automatically on uncovered misses. Daily or selected-day schedules use one token per missed occurrence. Weekly-target and monthly-target schedules use one token when a period ends below its target. Each occurrence or period is checked once: if no token is available then, the miss is unprotected and a later grant does not repair the streak. Vacation-covered dates and fully covered target periods do not spend tokens.
