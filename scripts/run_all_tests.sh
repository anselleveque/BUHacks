#!/bin/sh

cd "$(dirname "$0")/.." || exit 1

failed=0
if python3 -m pytest -q -p no:cacheprovider; then
    printf 'PASS pytest\n'
else
    printf 'FAIL pytest\n'
    failed=1
fi

if ! python3 -c 'from playwright.sync_api import sync_playwright' 2>/dev/null; then
    printf '%s\n' 'Playwright is missing or cannot be imported. Install requirements-dev.txt, then run python3 -m playwright install chromium.' >&2
    failed=1
fi

for walkthrough in web/tests/*walkthrough*.py; do
    if output=$(python3 "$walkthrough" 2>&1); then
        printf '%s\nPASS %s\n' "$output" "$walkthrough"
    else
        printf '%s\nFAIL %s\n' "$output" "$walkthrough"
        case $output in
            *"Executable doesn't exist"*)
                printf '%s\n' 'Playwright Chromium is missing. Run python3 -m playwright install chromium.' >&2
                ;;
        esac
        failed=1
    fi
done

exit "$failed"
