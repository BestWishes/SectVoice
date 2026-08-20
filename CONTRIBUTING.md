# Contributing

Thank you for helping improve SectVoice.

1. Open an issue for behavior changes, new engines or protocol changes before doing broad work.
2. Keep the Reader independent from engine-private payload formats.
3. Add focused tests and preserve all existing tests.
4. Run `pytest`, `compileall` and `git diff --check` locally. SectVoice intentionally does not use GitHub Actions or other hosted CI, so include the local verification performed in the pull request description.
5. For audio behavior, attach a reproducible text case, tier, package version and non-sensitive diagnostics. Do not upload a voice or recording unless you own it and intentionally authorize its publication.
6. Update user-facing documents and third-party notices when applicable.

Pull requests should be small enough to review, explain the user-visible result, list validation performed and disclose any untested hardware path.

By submitting a contribution, you agree that it is licensed under Apache License 2.0 and that you have the right to submit it. Do not contribute copied model weights, recordings, voices or code with incompatible licensing.
