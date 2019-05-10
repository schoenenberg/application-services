**See [the release process docs](docs/howtos/cut-a-new-release.md) for the steps to take when cutting a new release.**

# Unreleased Changes

[Full Changelog](https://github.com/mozilla/application-services/compare/v0.32.1...master)

## General

- All of our cryptographic primitives are now backed by NSS. This change should be transparent our customers.

## Push

### Breaking Changes

- `OpenSSLError` has been renamed to the more general `CryptoError`.
