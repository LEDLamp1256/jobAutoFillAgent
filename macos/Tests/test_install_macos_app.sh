#!/bin/zsh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
test_home="$(mktemp -d /private/tmp/jobagent-app-install-test.XXXXXXXX)"
trap 'rm -r "$test_home"' EXIT
export HOME="$test_home"
export JOBAGENT_SKIP_BOOTSTRAP=1
export JOBAGENT_BUILD_DIR="$test_home/swift-build"

app="$HOME/Applications/Job Application Agent.app"
print '#include <unistd.h>\nint main(){sleep(20);return 0;}' > "$test_home/running-app.c"
cc "$test_home/running-app.c" -o "$test_home/JobAgentControl"
"$test_home/JobAgentControl" &
running_pid=$!
sleep 1
if "$repo_root/Scripts/install-macos-app.sh" >/dev/null 2>&1; then
    print -u2 'Installer replaced a running application.'
    kill "$running_pid"
    exit 1
fi
kill "$running_pid"
wait "$running_pid" 2>/dev/null || true
[[ ! -e "$app" ]]

"$repo_root/Scripts/install-macos-app.sh" >/dev/null
[[ -d "$app" ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")" ==
    com.ledlamp1256.jobapplicationagent ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleName' "$app/Contents/Info.plist")" ==
    'Job Application Agent' ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundlePackageType' "$app/Contents/Info.plist")" == APPL ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$app/Contents/Info.plist")" == false ]]
[[ -x "$app/Contents/MacOS/JobAgentControl" ]]
[[ -s "$app/Contents/Resources/AppIcon.icns" ]]
[[ -f "$app/Contents/Resources/Backend/jobagent/control_plane_stdio.py" ]]
codesign --verify --strict "$app"
signature_details="$(codesign -dv "$app" 2>&1)"
[[ "$signature_details" == *'Identifier=com.ledlamp1256.jobapplicationagent'* ]]
[[ ! -e "$app/Contents/Resources/Backend/config.json" ]]
[[ ! -e "$app/Contents/Resources/Backend/applications.sqlite3" ]]

mkdir -p "$HOME/Library/Application Support/Job Application Agent"
print 'preserved state' > "$HOME/Library/Application Support/Job Application Agent/state-marker"
"$repo_root/Scripts/install-macos-app.sh" >/dev/null
[[ -d "$app" ]]
[[ "$(find "$HOME/Applications" -maxdepth 1 -type d -name 'Job Application Agent*.app' | wc -l | tr -d ' ')" == 1 ]]
[[ "$(cat "$HOME/Library/Application Support/Job Application Agent/state-marker")" == 'preserved state' ]]
[[ ! -e "$HOME/Applications/Job Application Agent v2.app" ]]
print 'Packaging install/update checks passed.'
