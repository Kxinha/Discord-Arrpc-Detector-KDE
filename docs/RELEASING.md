# Releasing

1. Bump `VERSION` in `discord-game-presence.py` and add a `## X.Y.Z` section on
   top of `CHANGELOG.md`. Its first line is what the tray's update notification
   shows.
2. Run the tests locally (see the README, Development) and commit.
3. Push `main` to both remotes and wait for the CI run on GitHub to pass:

   ```bash
   git push origin main && git push gitlab main
   gh run watch "$(gh run list -L1 --json databaseId -q '.[0].databaseId')" --exit-status
   ```

4. Tag and push the tag. GitHub Actions builds every package, checks
   `SHA256SUMS` and publishes the release (marked Latest) with all of `dist/`:

   ```bash
   git tag -a vX.Y.Z -m "X.Y.Z" && git push origin vX.Y.Z && git push gitlab vX.Y.Z
   ```

5. GitLab release with the same files the CI published:

   ```bash
   rm -rf /tmp/rel && gh release download vX.Y.Z -D /tmp/rel
   (cd /tmp/rel && sha256sum -c SHA256SUMS)
   glab release create vX.Y.Z /tmp/rel/* -R gabriel17166/discord-arrpc-detector-kde \
        --name "Discord Arrpc Detector KDE X.Y.Z" --notes-file <(awk -v v=X.Y.Z \
        '/^## / { if (f) exit; if ($2 == v) { f = 1; next } } f' CHANGELOG.md)
   ```

6. Check the public URLs: `install.sh` from both mirrors, the
   `releases/latest/download/SHA256SUMS` link, and the tray's update check
   (`discord-game-presence --check-update`).
