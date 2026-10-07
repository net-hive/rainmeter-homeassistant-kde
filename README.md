# Home Assistant widgets for KDE Plasma 6

Two native KDE Plasma 6 desktop widgets driven by Home Assistant, with no access token stored on the desktop. A port of Dustin Szabo's [rainmeter-homeassistant](https://github.com/VagueDustin/rainmeter-homeassistant) from Windows/Rainmeter to Linux, built and tested on CachyOS (Plasma 6, Wayland).

**HA Now Playing** shows album art, the title, and an artist line tinted with a colour picked from the artwork, for whatever is playing in HA: Music Assistant, Spotify, Sonos, Cast, Plex, Jellyfin, an AirPlay receiver, or anything else that is a `media_player`. It is only visible while something is playing or paused.

**HA Status Board** is a clock with two configurable rows underneath: up to 4 **stats** (a label over a value) and up to 6 **chips** (a coloured name over a sub-line). Every slot is just a string you build in Home Assistant YAML, so it can show a temperature, a person's presence, a door, a printer, or anything else. Empty slots hide, empty rows collapse, and with nothing configured it is simply a clock.

## How it works

The widgets never talk to the Home Assistant API, so they never need a token. Instead, HA writes small JSON files into its own `www/` folder, which it serves at `/local/` without authentication, and the widgets just read them:

```
Home Assistant  ──►  www/nowplaying.json      ──►  KDE widgets (poll over HTTP)
                     www/nowplaying_cover.png
                     www/statusboard.json
```

The trade-off: anyone who can reach your HA host can read `/local/`, so treat the current track and the status board as public within your LAN.

## What's here

```
kde/
  local.ha.nowplaying/         the now-playing widget
  local.ha.statusboard/        the clock / status board widget
  README.md                    widget install, settings, troubleshooting
homeassistant/
  scripts/nowplaying.py        fetches artwork, picks the accent, writes the JSON
  scripts/write_json.py        generic "render a template into www/" file writer
  packages/*.yaml              drop-in HA config: source picker, sensors, refresh
docs/                          upstream HA-side docs (install, sources, troubleshooting)
skins/                         the original Rainmeter skins, kept from upstream
```

## Differences from upstream

- **Native KDE widgets** in `kde/` instead of Rainmeter skins. Every `[Variables]` setting from the skins is a field in the widget's settings dialog.
- **Fixed for Home Assistant 2026.9.** Both HA sensors failed on current HA, so neither JSON file was ever written:
  - Status board: the payload is now a native object, and `base64_encode` rejected it (`a bytes-like object is required, not 'Wrapper'`). It is now converted with `to_json` first.
  - Now playing: a multi-line `--artist-b64` argument left a newline in the shell command, which cut it in half (`return code 127`). It is now on one line.

Both fixes are also offered upstream in [#3](https://github.com/VagueDustin/rainmeter-homeassistant/pull/3), and the widgets in [#4](https://github.com/VagueDustin/rainmeter-homeassistant/pull/4).

## Setup

### 1. Home Assistant side

Run these in HA's **Terminal & SSH** add-on.

1. Copy the scripts and packages into place:
   ```sh
   cd /tmp && curl -L https://github.com/net-hive/rainmeter-homeassistant-kde/archive/refs/heads/main.tar.gz | tar xz
   mkdir -p /config/rainmeter /config/packages /config/www
   cp rainmeter-homeassistant-kde-main/homeassistant/scripts/*.py /config/rainmeter/
   cp rainmeter-homeassistant-kde-main/homeassistant/packages/*.yaml /config/packages/
   ```
2. Make sure `configuration.yaml` loads packages. If it already has a `homeassistant:` section, add the `packages:` line under it rather than adding a second section:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
3. Edit the two package files in `/config/packages/`:
   - `rainmeter_nowplaying.yaml`: set `--ha-base` to the URL **your desktop** uses to reach HA, including the port if it is not 80 (for example `http://192.168.1.10:8123`). It is baked into the cover art URL, so `127.0.0.1` will not work. Add any players you never want shown (a TV, say) to `exclude`.
   - `rainmeter_statusboard.yaml`: the shipped stats and chips are examples (`climate.house`, `person.alex`, ...). Replace them with your own entities, or empty the `stats` and `people` lists to start with a plain clock. See [Customising the status board](#customising-the-status-board).
4. **Check configuration** under Developer tools → YAML, then do a full **Restart** (not a reload) the first time, so the `command_line` sensors are created.
5. Open both files in a browser on the desktop. Each should show JSON:
   - `http://<your-ha>/local/statusboard.json`
   - `http://<your-ha>/local/nowplaying.json`

### 2. KDE side

```sh
git clone https://github.com/net-hive/rainmeter-homeassistant-kde.git
cd rainmeter-homeassistant-kde
kpackagetool6 -t Plasma/Applet -i kde/local.ha.nowplaying
kpackagetool6 -t Plasma/Applet -i kde/local.ha.statusboard
systemctl --user restart plasma-plasmashell
```

Then right-click the desktop, **Enter Edit Mode**, **Add Widgets**, search for `HA`, and drag each widget onto the desktop. Right-click each one, **Configure**, and set **Home Assistant URL** to the same base as `--ha-base`.

To update later, `git pull` and run the same commands with `-u` instead of `-i`. Full widget details are in [kde/README.md](kde/README.md).

## Customising the status board

What the board shows is decided entirely in `/config/packages/rainmeter_statusboard.yaml`. The widget just renders whatever arrives.

- **Stats** (up to 4) go in the `set stats = [` list, each as `('LABEL', value, COLOUR)`:
  ```
  ('OFFICE',
   states('sensor.office_temperature') ~ '°',
   WHITE),
  ```
- **People** (up to 6 chips, green when home, red when away, with today's arrival time) go in the `set people = [` list:
  ```
  ('person.someone', 'NAME'),
  ```
- Chips do not have to be people. The comments in the file show doors, printers and backups.
- Also add every entity you use to the short list at the top of the file (the `{{ [ states(...), ... ] }}` fingerprint). That is what makes the board refresh the moment a value changes. Without it, the board still catches up on its 5-minute heartbeat.

After editing, reload **Template entities** under Developer tools → YAML. No restart needed. The board updates within about 15 seconds.

## Using a source that isn't a `media_player`

The now-playing picker only looks at `media_player` entities. If your music shows up in HA some other way, such as a template sensor fed by a phone app, you can add it as a fallback: in `rainmeter_nowplaying.yaml`, change the last line of the source picker from `{{ ns.pick if ns.pick else 'none' }}` to also return your sensor when its state is `playing` or `paused`. Then add `or state_attr(..., 'title')` style fallbacks wherever the command reads `media_title`, `media_artist` and `media_album_name`, using your sensor's attribute names. A template sensor with a `picture:` set provides the artwork automatically.

## Troubleshooting

- **Status board says "Waiting for Home Assistant".** It cannot fetch `statusboard.json`, or the file's timestamp is more than 15 minutes old. Open the file's URL from the desktop: a **404** means HA is not writing it, and a **connection error** means the widget's URL or port is wrong.
- **404 on the JSON files.** Look in Developer tools → States for `sensor.rainmeter_status_board` and `sensor.rainmeter_now_playing`. If they are missing, the packages did not load (check `configuration.yaml` and restart). If they are `unknown`, run `ha core logs | grep -i command_line` in the HA terminal to see why the script failed.
- **`.local` hostnames fail although they resolve.** Some networks resolve `homeassistant.local` to a link-local IPv6 address first, which the widgets cannot connect to. Use HA's IPv4 address instead.
- **Now Playing never appears.** It hides unless something is playing or paused. Check that `sensor.rainmeter_source_entity` names the player you expect while music plays. If it says `none`, your player is excluded, has no `media_title`, or is not a `media_player`.
- **A value shows as `unknown°`.** The entity behind that stat has no value in HA. Pick a different sensor, or remove the stat.

## Requirements

- KDE Plasma 6
- Home Assistant with `www/` served at `/local/` (the default) and `command_line` available
- Pillow on the HA box for artwork and accent colour (already in the official container)
- The Roboto font, or change **Font** in each widget's settings

## Credits

Original Rainmeter skins, Home Assistant package and design by [Dustin Szabo](https://github.com/VagueDustin/rainmeter-homeassistant). KDE Plasma 6 port and HA 2026.9 fixes by Jason Huang. MIT licensed. See [LICENSE](LICENSE).
