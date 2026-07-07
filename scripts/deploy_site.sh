#!/usr/bin/env sh
# Deploy site/ (as committed on HEAD) to GitHub Pages:
#   gh-pages branch on the public origin -> https://lesani.github.io/CueForge/
# Commit site/ changes first; this publishes HEAD:site, not the working tree.
set -e

tree=$(git rev-parse HEAD:site)
commit=$(git commit-tree "$tree" -m "Deploy site $(git rev-parse --short HEAD)")
git push origin "$commit:refs/heads/gh-pages" --force
echo "[OK] GitHub Pages: pushed $commit to origin/gh-pages"
