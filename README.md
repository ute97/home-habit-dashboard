# Home Habit Dashboard

A private, self-hosted habit, task, and goal tracker for Home Assistant OS. The app has five responsive views: Today, Habit Grid, Tasks, Goals, and Insights. Each household member can use a shared profile picker; profiles are not separate logins.

## Installation on Home Assistant OS

This is a **Home Assistant custom app repository**, not a HACS integration. HACS does not install or manage standalone app containers. You do not need approval from Nabu Casa to add a custom repository URL manually; inclusion in an official/default catalog is a separate, optional process.

1. Create a public GitHub repository named `home-habit-dashboard` and upload this project to its default branch.
2. The repository and app manifests are already configured for `ute97/home-habit-dashboard`.
3. In **Settings → Actions → General**, allow workflows to write packages (the build workflow requests `packages: write`). Publish a GitHub Release tagged `0.1.3` (matching `habit-dashboard/config.yaml`). The workflow builds `aarch64` and `amd64` images and publishes a multi-architecture image to GHCR. Set the resulting GHCR package visibility to **Public** so your Pi can download it without credentials.
4. In Home Assistant, open **Settings → Apps → App Store → ⋮ → Repositories**, enter `https://github.com/ute97/home-habit-dashboard`, and add it.
5. Install **Home Habit Dashboard** from the App Store. Set the app's `timezone` option to your IANA timezone (for example `Europe/Amsterdam`) before starting it.
6. Open the app from the Home Assistant sidebar. Home Assistant ingress and your existing Home Assistant authentication provide access; no port forwarding or long-lived access token is needed.

Create future releases to publish an updated image. Update the app version in `habit-dashboard/config.yaml` to match each release tag. Install updates from the app page in Home Assistant.

### Removing the app

Stop and uninstall it from its Home Assistant app page. Keep the app's data if you may reinstall or restore it; deleting the app's data removes its SQLite database. The UI also offers a JSON export before uninstalling. Test a Home Assistant backup before relying on it as your only copy.

## Local development

The API and UI use Python's standard library and SQLite; the application itself has no Python package dependencies.
```powershell
$env:DATA_DIR = "$PWD\data"
python .\habit-dashboard\server.py
```

Open `http://localhost:8099`. To run the API tests:

```powershell
python -m unittest discover -s .\habit-dashboard\tests -v
```

Build a container locally with Docker from `habit-dashboard`:

```powershell
docker build -t home-habit-dashboard:dev .\habit-dashboard
```

See [the app documentation](./habit-dashboard/DOCS.md) for data storage, timezone, privacy, and freeze-token rules.

## Product boundaries

- This is a standalone tracker; it does not import Home Assistant chores or create HA entities.
- A shared profile selector keeps lists and history separate, but does not hide profile data from other dashboard users.
- Freeze tokens are granted manually. Missed periods are evaluated once at period end; tokens added later do not retroactively repair a missed streak.
- Export a backup before importing: import replaces all tracker data.
- The app uses no external fonts, analytics, or cloud sync.
