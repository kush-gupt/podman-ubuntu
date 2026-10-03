# Signing key management

## Design

- **Master key**: generated offline, never leaves your machine (ideally on a
  hardware token). Only used to mint/rotate/revoke subkeys.
- **CI subkey**: signing-only, **no passphrase**, exported base64 into the
  `APT_SIGNING_SUBKEY` Actions secret. CI imports it at publish time.
- Blast radius of a leak: an attacker can sign repository metadata until the
  subkey is revoked. They cannot certify new keys or modify the master key.
- Subkey expiry: 1 year. Rotation is a calendar event, documented below.

## Generate (do this on your own machine, not in CI)

```sh
# 1. Master key (certify-only), 2-year expiry
gpg --batch --quick-generate-key "podman-ubuntu APT Signing" ed25519 cert 2y

# 2. Signing subkey, no passphrase, 1-year expiry
gpg --batch --quick-add-key <MASTER_FPR> ed25519 sign 1y
# (press Enter at the passphrase prompt to leave it empty)

# 3. Revocation certificate for the subkey (store with your master backup)
gpg --output podman-ubuntu-signing.revoke --gen-revoke <SUBKEY_FPR>

# 4. Public key -> commit to the repo
gpg --armor --export <MASTER_FPR> > keys/apt-signing.asc

# 5. Subkey -> Actions secret (base64, single line)
gpg --armor --export-secret-subkeys <SUBKEY_FPR> | base64 -w0
# Paste the output as the APT_SIGNING_SUBKEY repository secret.

# 6. Publish the fingerprint somewhere public (README, release notes) so
# users can verify out-of-band.
gpg --list-keys --with-colons <MASTER_FPR> | awk -F: '/^fpr:/ {print $10}'
```

## Rotate (yearly, or on suspected compromise)

1. Generate a new signing subkey from the master key (step 2 above).
2. If compromised: publish the revocation certificate from step 3.
3. Replace the `APT_SIGNING_SUBKEY` secret.
4. Replace `keys/apt-signing.asc` and open a PR.
5. The next publish signs with the new subkey. Old `InRelease` files remain
   verifiable against the old public key in git history.

## Verification for users

```sh
curl -fsSL https://kush-gupt.github.io/podman-ubuntu/keys/apt-signing.asc \
  | gpg --show-keys --with-colons
# Compare the fingerprint with the one published in the README/releases.
```
