# Shared debhelper overrides for podman-ubuntu packages.
# Included by each component's debian/rules via `include debian/common/rules.mk`
# (copied to debian/common/ by scripts/prepare-source.sh so the source tree
# is self-contained).

# Skip dbgsym generation (not needed for our APT repo; avoids lintian
# skipping .changes when the .ddeb is missing).
override_dh_strip:
	dh_strip --no-automatic-dbgsym
