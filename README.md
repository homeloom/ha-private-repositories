# Private Repositories

Install and update Home Assistant custom integrations from **private GitHub
repositories**, entirely from the Home Assistant UI.

[HACS](https://hacs.xyz) only reads public repositories. Private Repositories
covers the private ones: add a GitHub token once, pick repositories, and each
integration gets an update entity like any other part of Home Assistant.

## Features

- **Set up in the UI.** One GitHub token, any number of repositories, no file to
  edit.
- **Native updates.** One update entity per integration, in
  *Settings › System › Updates*, with release notes, an *Install* button and a
  progress bar. Older releases can be installed with the `update.install`
  action (see [Updates](#updates)).
- **Releases drive updates.** New GitHub releases show up on their own, checked
  every 15 minutes. Unchanged answers do not count against GitHub's rate limit.
- **Restart when needed.** After an install, a repair offers to restart Home
  Assistant. It checks the configuration first.
- **Token reminders.** A sensor shows when the token expires, and a repair
  appears 30 days before, with the steps to renew it and a field to paste the
  new one.
- **Careful installs.** Archives are checked before the integration's folder is
  touched, and the new version is swapped in at once. If Home
  Assistant stops in the middle of an install, the previous version is put back
  on the next start, and a repair asks for one more restart to load it.

## Installation

Private Repositories itself is a custom integration. It requires Home Assistant
2025.6.1 or newer.

- **With HACS:** in HACS, open the menu › *Custom repositories*, add
  `https://github.com/homeloom/ha-private-repositories` with the type
  *Integration*, then download *Private Repositories*.
- **Manually:** copy `custom_components/private_repositories` into the
  `custom_components` folder of your Home Assistant configuration.

Restart Home Assistant, then go to *Settings › Devices & services ›
Add integration* and pick **Private Repositories**.

## Create a GitHub token

Use a fine-grained personal access token, limited to what it needs. Classic
tokens are refused: they cannot be limited to reading a few repositories.

1. On GitHub, open *Settings › Developer settings › Personal access tokens ›
   Fine-grained tokens › [Generate new token](https://github.com/settings/personal-access-tokens/new)*.
2. **Resource owner:** the account or organization that owns the repositories.
   Fine-grained tokens only reach repositories owned by you or by an
   organization you belong to, not repositories where you are only a
   collaborator.
3. **Expiration:** up to one year. Home Assistant reminds you before it ends.
4. **Repository access:** *Only select repositories*, then the repositories to
   track.
5. **Permissions:** *Repository permissions › Contents: Read-only*. Nothing
   else (*Metadata: Read-only* is added by GitHub).
6. Generate the token and paste it in Home Assistant.

If the owner is an organization, it may have to approve the token first
(organization *Settings › Personal access tokens › Pending requests*). Until
then, its repositories cannot be found.

Repositories owned by different accounts need one token each: add the
integration once per token.

## Track a repository

On the Private Repositories entry, choose **Add repository**, then pick a
repository from the list (or type `owner/name`, or paste its GitHub address).

- If its integration is not installed yet, the latest release is installed
  right away, and a repair offers to restart Home Assistant. The dialog shows
  the install while it runs. You can close it: the install goes on, and the
  repository is tracked when it ends.
- If it is already installed, by hand or by another tool, you are asked to
  confirm before Private Repositories takes it over. Nothing is changed then:
  the update entity offers the latest release when it is newer. Closing the
  dialog before you confirm leaves the integration as it is, untracked.

A repository is refused if its integration is part of Home Assistant, or is
already tracked from another repository. It is also refused if
`custom_components` already has a folder of that name that is not an integration
with a readable `manifest.json` and version: fix or remove that folder first.

Enable **Include pre-releases** to be offered releases marked as pre-releases.
To change it later, choose **Reconfigure** on the repository.

A repository is followed by its GitHub id, not by its name. If it is renamed,
or its account is, it keeps its updates and Home Assistant shows the new name.
A repository created later under the old name is never used.

## What a repository must provide

- **GitHub releases.** Each release's tag is `v1.2.3` or `1.2.3`, the same as
  `version` in the integration's `manifest.json`. The release notes are shown in
  Home Assistant. Draft releases are ignored.
- **One integration**, in `custom_components/<domain>/`, as HACS expects. Its
  `manifest.json` has a `domain`, a `name`, and a `version` that Home Assistant
  accepts.
- Optionally, a `hacs.json` with the HACS options that change how the
  integration is shipped:

  | Option | Effect |
  | --- | --- |
  | `content_in_root` | The integration's files are at the root of the repository. |
  | `zip_release` and `filename` | Install the zip file attached to the release instead of the source archive. |
  | `persistent_directory` | A folder inside the integration kept across updates. |
  | `homeassistant` | The oldest Home Assistant version the release supports. |

  The `persistent_directory` is copied into the new version when it is
  installed: what the running version writes there until Home Assistant
  restarts is not carried over. A `zip_release` asset is compressed with
  *deflate*, the usual method, or not at all: other methods are refused.

### Limits

An install is refused when:

- the download is over 50 MB. Without `zip_release`, the download is GitHub's
  source archive of the whole repository at the release's tag, with its
  documentation and images;
- the archive holds more than 10,000 entries (files and folders), or more than
  1 GB once unpacked;
- the integration itself is over 200 MB once unpacked, or its `manifest.json`
  is over 1 MB;
- the disk has less free space than twice the size of the integration, plus
  50 MB.

A repository with large files next to its integration can attach a zip of the
integration to each release and set `zip_release` and `filename` in
`hacs.json`. It can also leave those files out of the source archive with
`export-ignore` in `.gitattributes`.

Updates are looked for among the newest 100 releases. An older release can
still be installed by its version (see [Updates](#updates)).

## Updates

Updates appear in *Settings › System › Updates*. *Install* downloads the
release, checks it, and replaces the integration. Home Assistant loads the new
version when it restarts: use the repair it shows, or restart later. Restart
soon: until then the old version runs, but a part of the integration it had
not loaded yet, such as a platform set up later, would come from the new
files. To check for new releases at once, call the
`homeassistant.update_entity` action on the update entity.

Installs run one at a time: an install waits for the one before it, and shows
as in progress meanwhile. A second install of the same integration is refused
until the first one ends.

To go back to an older release, call the `update.install` action with its
version:

```yaml
action: update.install
target:
  entity_id: update.my_integration
data:
  version: "1.2.0"
```

## Renew the token

Before the token expires, a repair explains how to renew it. On GitHub, open
the token, choose **Regenerate token**, copy the new one and paste it in the
repair. Its repositories and permissions stay the same.

You can also replace a token at any time with **Reconfigure** on the entry. The
new token must belong to the same GitHub account and read the same
repositories; to use another account, add the integration again. If a token
expires anyway, Home Assistant asks for a new one.

A new token is refused while a tracked repository cannot be read with it. If a
repository was deleted or moved to another owner, remove it from Private
Repositories first.

The entry is named after the GitHub account. It keeps that name if the account
is renamed on GitHub; rename it from its menu if you like.

## Options

On the entry, choose **Configure**.

| Option | Default |
| --- | --- |
| Remind me before the token expires | 30 days (1 to 90) |

## Remove

Removing a repository, or the whole entry, stops tracking it. Installed
integrations stay in place: remove them from *Settings › Devices & services*
and delete their folder from `custom_components` if you no longer want them.

## Security

- The token is stored by Home Assistant with its other credentials, and sent
  only to `api.github.com`. Archives are then downloaded from GitHub's own
  download hosts, through short-lived links, without the token.
- An integration runs inside Home Assistant with full access to it. Only track
  repositories you trust, and give the token read access to nothing else.
- An integration should be managed by one tool only: do not track with Private
  Repositories an integration that HACS also manages.
- Diagnostics leave out the token, the account and the names of the
  repositories.

## Development

CI uses the shared workflows of [homeloom/ci](https://github.com/homeloom/ci):
the hygiene checks, Ruff, hassfest, and the tests on the oldest supported Home
Assistant (the `homeassistant` key of `hacs.json`) and on the latest one, with
at least 95 % coverage. [CONTRIBUTING.md](CONTRIBUTING.md) explains how changes
are proposed and released.

To run the checks against the latest Home Assistant, on Python 3.14.2 or newer:

```sh
python3.14 -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component ruff
ruff check . && ruff format --check .
pytest --cov=custom_components
```

## License

[MIT](LICENSE)
