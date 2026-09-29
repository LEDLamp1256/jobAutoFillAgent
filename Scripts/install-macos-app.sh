#!/bin/zsh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_name='Job Application Agent.app'
bundle_id='com.ledlamp1256.jobapplicationagent'
install_dir="$HOME/Applications"
destination="$install_dir/$app_name"
build_dir="${JOBAGENT_BUILD_DIR:-$HOME/Library/Caches/Job Application Agent/swift-build}"

if pgrep -x JobAgentControl >/dev/null 2>&1; then
    print -u2 'Quit Job Application Agent (including any swift run copy) before installing.'
    exit 1
fi

if [[ "${JOBAGENT_SKIP_BOOTSTRAP:-0}" != 1 ]]; then
    "$repo_root/Scripts/bootstrap-macos-runtime.sh"
fi

mkdir -p "$build_dir" "$install_dir"
swift build -c release --scratch-path "$build_dir" --package-path "$repo_root"
executable="$build_dir/release/JobAgentControl"
[[ -x "$executable" ]] || { print -u2 'Swift did not produce the JobAgentControl executable.'; exit 1; }

stage="$(mktemp -d "$install_dir/.jobagent-stage.XXXXXXXX")"
backup=''
cleanup() {
    if [[ -n "$backup" && -d "$backup" && ! -e "$destination" ]]; then
        mv "$backup" "$destination"
    fi
    if [[ -d "$stage" ]]; then
        rm -r "$stage"
    fi
}
trap cleanup EXIT

bundle="$stage/$app_name"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources/Backend/jobagent"
cp "$executable" "$bundle/Contents/MacOS/JobAgentControl"
cp "$repo_root/macos/Info.plist" "$bundle/Contents/Info.plist"
cp "$repo_root/macos/AppIcon.icns" "$bundle/Contents/Resources/AppIcon.icns"
cp "$repo_root"/jobagent/*.py "$bundle/Contents/Resources/Backend/jobagent/"

plist="$bundle/Contents/Info.plist"
plutil -lint "$plist" >/dev/null
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist")" == "$bundle_id" ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$plist")" == JobAgentControl ]]
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$plist")" == AppIcon.icns ]]
[[ -s "$bundle/Contents/Resources/AppIcon.icns" ]]
[[ -f "$bundle/Contents/Resources/Backend/jobagent/control_plane_stdio.py" ]]
[[ -x "$bundle/Contents/MacOS/JobAgentControl" ]]
if find "$bundle" -type f \( -name 'config.json' -o -name '*.sqlite*' -o -name '*auth*.json' \) | grep -q .; then
    print -u2 'Private configuration or database content was found in the app bundle.'
    exit 1
fi
codesign --force --sign - --identifier "$bundle_id" "$bundle" >/dev/null
codesign --verify --strict "$bundle"

if pgrep -x JobAgentControl >/dev/null 2>&1; then
    print -u2 'Job Application Agent started while building. Quit it and retry the install.'
    exit 1
fi
if [[ -e "$destination" ]]; then
    [[ -d "$destination" && ! -L "$destination" ]] || {
        print -u2 'The canonical app path is not a regular app bundle; refusing replacement.'
        exit 1
    }
    backup="$(mktemp -d "$install_dir/.jobagent-backup.XXXXXXXX")"
    rmdir "$backup"
    mv "$destination" "$backup"
fi
mv "$bundle" "$destination"
if [[ -n "$backup" ]]; then
    rm -r "$backup"
    backup=''
fi
print "Installed $destination"
