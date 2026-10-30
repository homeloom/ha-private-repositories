# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A brand icon, shown by Home Assistant 2026.3 and later, and by HACS.

## [0.1.0]

### Added

- Install and update custom integrations from private GitHub repositories,
  set up in the UI: one entry per fine-grained token, one subentry per
  repository.
- One update entity per integration, following GitHub releases, with release
  notes, a progress bar, pre-releases on request, and older releases through
  the `update.install` action. Installs run one at a time, and show as in
  progress while they wait.
- A progress dialog when a repository is added. The install goes on if the
  dialog is closed.
- A repair that offers to restart Home Assistant after an install, and
  recovery of an install interrupted by a stop.
- A sensor showing when the token expires, and a repair before it does, with
  the steps to renew it.
- Repositories followed by their GitHub id, so that a renamed repository keeps
  its updates.
- The HACS options `content_in_root`, `zip_release`, `filename`,
  `persistent_directory` and `homeassistant`.
- Diagnostics without the token, the account or the names of the
  repositories.
