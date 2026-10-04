# Shared bits for the Rust components (netavark, aardvark-dns): the builder
# image vendors crates offline, and the toolchain lives under /root.
# Included by each Rust component's debian/rules via
# `include debian/common/rust.mk`.

export CARGO_NET_OFFLINE := true
# The Actions runner overrides HOME but the rustup toolchain lives under
# /root in the builder image; point rustup back explicitly.
export RUSTUP_HOME ?= /root/.rustup
export CARGO_HOME ?= /root/.cargo

# dh_clean deletes *.orig recursively, but cargo's vendored .orig files are
# required for the offline build. Stash vendor outside the tree during clean.
override_dh_clean:
	if [ -d vendor ]; then mv vendor ../vendor.stash; fi
	dh_clean || true
	if [ -d ../vendor.stash ]; then mv ../vendor.stash vendor; fi
