#!/bin/sh
# Publish HEAD to github.com/gabriel-pauta/clipwatch with a clean layout:
# the program under app/, only README, license, start.bat and images at the root.
# The local checkout keeps its flat layout (running processes and .venv live there).
# Python modules stay together in app/: they import each other as top-level names.
set -e
cd "$(dirname "$0")/.."
export GIT_INDEX_FILE="$(mktemp)"
git read-tree --empty
git read-tree --prefix=app/ HEAD
git rm -r -q --cached app/.github app/README.md app/docs/clipwatch
git read-tree --prefix=images/ HEAD:docs/clipwatch
for f in README.md LICENSE NOTICE; do
  git update-index --add --cacheinfo "100644,$(git rev-parse HEAD:$f),$f"
done
WRAP=$(printf '@echo off\r\nREM Clipwatch: runs app\\start.bat (backend + console at http://localhost:8000/console)\r\ncall "%%~dp0app\\start.bat" %%*\r\n' | git hash-object -w --stdin)
git update-index --add --cacheinfo "100644,$WRAP,start.bat"
TREE=$(git write-tree)
rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE
git fetch -q clipwatch main
C=$(git commit-tree "$TREE" -p FETCH_HEAD -m "${1:-Update Clipwatch}")
git push -q clipwatch "$C:refs/heads/main"
echo "pushed $C"
