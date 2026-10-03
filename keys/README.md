# keys/

This directory holds the **public** repository signing key.

- `apt-signing.asc`: the ASCII-armored public key users add to apt's
  `Signed-By`. Committed to the repo by design.

Generate it by following [docs/SIGNING.md](../docs/SIGNING.md). The private
master key and the CI subkey must never be committed here (`.gitignore`
blocks `*.gpg`/`*.asc` except this one file — keep it that way).
