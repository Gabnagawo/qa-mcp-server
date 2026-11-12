# Demo: exploratory testing of Markor with an AI agent

This script drives a real device. The target is **Markor**
(`net.gsantner.markor`), an open-source Markdown and text editor distributed on
F-Droid (<https://f-droid.org/packages/net.gsantner.markor/>, Apache-2.0).

> Status: this real-device run is **to be measured**. No Android device was
> attached where this repository was built, so the sequence below is the
> *expected* tool-call flow, not a recorded transcript. The same flow, run
> against the scripted fake device, is recorded in the README and in
> [results/demo-transcript.txt](results/demo-transcript.txt).

## 1. Prepare a disposable device

Use an emulator or a test phone with no personal accounts (see
[SECURITY.md](SECURITY.md), residual risk).

```bash
# Android SDK command-line tools installed; any recent system image works
avdmanager create avd -n qa-api34 -k "system-images;android-34;google_apis;x86_64"
emulator -avd qa-api34 -no-snapshot &
adb wait-for-device && adb devices -l
```

## 2. Get the APK from F-Droid

Download the latest Markor APK from its F-Droid page into the server's APK
directory. F-Droid names files `<package>_<versionCode>.apk`, which already
meets the file-name rule (`[A-Za-z0-9._-]+.apk`).

```bash
mkdir -p ~/qa-apks
# browser download, or:  curl -L -o ~/qa-apks/net.gsantner.markor_<versionCode>.apk <link from the F-Droid page>
```

## 3. Configure the server

`~/qa-mcp.toml`:

```toml
apk_dir = "/home/me/qa-apks"
artifacts_dir = "/home/me/qa-artifacts"
allowed_packages = ["net.gsantner.markor"]
```

Register it with Claude Code:

```bash
claude mcp add qa-android -- uv --directory /path/to/qa-mcp-server run qa-mcp-server serve --config /home/me/qa-mcp.toml
```

(Claude Desktop configuration is in the [README](../README.md#use-it-from-claude).)
For a first run, add `--dry-run` and inspect `qa-artifacts/audit.jsonl` to see
what the agent *would* have done.

## 4. Prompt

> You are testing the Android app Markor (net.gsantner.markor) on the connected
> device. Install the Markor APK from the APK directory, launch it, and do a
> 15-minute exploratory session on note creation: create a Markdown file, type
> a title and a short body with a list, save it, rename it, reopen it, and try
> the share/export actions. Before every interaction read the screen with
> dump_ui and use selectors from it. After each save, rename and export, check
> logcat at level E for the app. Take a screenshot as evidence of anything
> unexpected. Finish with a bug report: steps, expected, actual, evidence.

## 5. Expected sequence of tool calls

Element labels below are what the agent should look for. The exact `text`,
`resource_id` and `content_desc` values come from `dump_ui` on the device under
test; they are not hard-coded here.

| # | Tool call | Purpose | Expected observation |
|---|---|---|---|
| 1 | `list_devices()` | Find the target | one device in state `device` |
| 2 | `install_apk(apk_path="net.gsantner.markor_<versionCode>.apk")` | Install from the sandboxed APK dir | `installed: true`, sha256 of the APK |
| 3 | `launch_app(package="net.gsantner.markor")` | Launcher activity resolved automatically | `status: ok`, `launch_state: COLD`, `total_time_ms` = cold-start time |
| 4 | `dump_ui()` | Read the first screen | first-run intro or the file browser |
| 5 | `tap(text=...)` / `press_key(key="BACK")` | Dismiss the intro, if shown | file browser with a "create new" button |
| 6 | `tap(content_desc=...)` | Open the "new file" dialog | dialog with a name field and a type selector |
| 7 | `tap(resource_id=...)`, `input_text(text="qa session notes")` | Name the file | text appears in the field (`dump_ui`) |
| 8 | `tap(text="OK")` | Create the file | editor screen |
| 9 | `input_text(text="Groceries")`, `press_key(key="ENTER")`, `input_text(text="- milk")` | Type a body | editor text updated |
| 10 | `press_key(key="BACK")` then `dump_ui()` | Leave the editor (auto-save) | file listed in the browser |
| 11 | `logcat(package="net.gsantner.markor", level="E")` | Crash check after save | expected: 0 lines |
| 12 | `tap(...)` on the file, overflow menu, rename, `input_text(...)` | Rename | new name listed |
| 13 | `logcat(package=..., level="E")` | Crash check after rename | |
| 14 | open the file, the share/export action, `dump_ui()` | Exercise share/export | system share sheet appears |
| 15 | `press_key(key="BACK")`, `logcat(level="E", since="<time of step 14>")` | Crash check after export | |
| 16 | `screenshot(save=true)` | Evidence for any anomaly | PNG path under `artifacts_dir/screenshots/` |
| 17 | final answer | Bug report or "no defects found" with the steps covered | |

What a reviewer checks afterwards:

- `audit.jsonl` has one line per call, so the session can be replayed by hand.
- No `rejected` outcomes, unless the agent tried something outside the allowlist.
  Those lines show what it tried.
- Cold-start time from step 3, and whether any `E/AndroidRuntime` lines appeared.

## Results

| Metric | Value |
|---|---|
| Tool calls in the session | to be measured |
| Cold start (`TotalTime`, step 3) | to be measured |
| Defects found | to be measured |
| Tool-call latency on a real device | to be measured |
