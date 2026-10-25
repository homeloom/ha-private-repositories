# Contributing

Thank you for helping. This document explains how changes are proposed,
checked and released.

## Ground rules

- Everything is written in English: code, comments, documentation, commit
  messages, pull requests and issues. Translations live in translation files
  only.
- Nothing personal goes in the repository or its history: no names, home
  addresses, network addresses, device identifiers, tokens or screenshots
  that identify a home. Use `example.com`, the documentation IP ranges
  (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and placeholder
  values in examples and tests.
- Commits use a noreply e-mail address
  ([GitHub documentation](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address)).
- Keep changes small and focused. One pull request does one thing.

## Workflow

1. Open an issue first for anything larger than a small fix, so the design
   can be agreed before the work starts.
2. Create a branch and open the pull request as a **draft** right away. No CI
   job runs on drafts, which saves runner minutes while the work is in
   progress: run the checks below locally meanwhile.
3. Give the pull request a [Conventional Commits](https://www.conventionalcommits.org/)
   title, such as `feat: add a sensor` or `fix(api): handle timeouts`. The
   title becomes the commit message when the pull request is squash merged.
4. Fill in the pull request template, including how the change was tested.
5. When the change is complete, mark the pull request as ready for review.
   CI runs once, and the **Report** check must pass before merging.
6. Answer every review comment, either with a change or with an explanation.

Pushed commits stay on GitHub even after a force push. If personal data or a
secret was pushed, do not try to hide it: tell a maintainer, and revoke the
secret.

## Tests and quality

- New code comes with tests that check real behavior. Total coverage must
  stay at or above 95 %. Code is only left out with
  `# pragma: no cover - <reason>` on the line, and lowering the threshold
  needs a written justification in the pull request.
- Linting and formatting use Ruff with the configuration of the repository.
- Do not add a dependency without checking its license and maintenance
  status. CI rejects licenses outside the allowed list.

## Changelog and releases

Every user facing change adds a line to `CHANGELOG.md` under
`## [Unreleased]`, following [Keep a Changelog](https://keepachangelog.com/).

To release, a pull request bumps the version (in the integration manifest,
app configuration or `pyproject.toml`) and renames the `Unreleased` section
to that version. Once it is merged and CI passes on `main`, a release tagged
`v<version>` is published automatically with that section as release notes.
Versions follow [Semantic Versioning](https://semver.org/).

This repository has no release workflow yet: the pull request of the first
release, 0.1.0, adds it. Until then nothing is published, and every change
stays under `Unreleased`.
