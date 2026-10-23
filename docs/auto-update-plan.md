# Update notification: design and verification

Status: implemented. Notification only. No silent update.

## Decision

The tool checks GitHub at session start and reports a newer release. The user
runs the update command and approves the hooks again. Nothing replaces the
script without user action.

## Why not silent

Codex binds hook trust to the command and to the content of the files it
resolves. Verified with codex-cli 0.164.0:

- One comment added to the installed script changed `trustStatus` from
  `trusted` to `modified`. Codex skips a modified hook.
- `codex plugin marketplace upgrade` changed every plugin hook to `modified` or
  `untrusted`. The new version also changes the versioned cache path.

A self-updating script would disable its own hook. A stable launcher could keep
trust, but it would run code downloaded after approval. This project does not
need that tradeoff.

Writing hash values into `[hooks.state]` is rejected. It forges approval.

## Behaviour

- `SessionStart` calls `update_notice`. It runs for every session, with or
  without a pending resume.
- The check requests `https://github.com/IsmailAzzouz/codex-auto-startstop/releases/latest`.
  GitHub redirects to the current tag. One HTTPS request. No API token. No API
  rate limit.
- The result is cached in `~/.codex/auto-startstop/update.json` for
  `update_check_hours` (default 24).
- The request must use HTTPS. A redirect must stay on the configured host.
  Plain HTTP is allowed only on loopback, for tests. Any other redirect raises
  and the check fails open.
- A failed check, including a final URL that is not a release tag, is logged
  and retried after one hour. A failure is not cached as "no update".
- A malformed `update_check_hours` falls back to 24 hours. A malformed
  `checked_at` falls back to zero, so the next session retries the check.
- The state read-modify-write runs under the existing `state_lock`. The network
  request runs outside the lock. The lock is a no-op on Windows, like `state.json`.
- A newer version produces one `systemMessage` for the user and one
  `additionalContext` entry for the model. The notice appears once per version.
- The update command depends on the install path. Plugin installs use
  `codex plugin marketplace upgrade codex-auto-startstop`. Other installs use
  `git pull, then run python3 codex_auto_startstop.py install`.
- The notice states that Codex asks for hook approval again.
- The check fails open. A timeout, a closed port, or a bad URL never breaks the
  session.
- `status` shows a cached newer version without a network request.

## Configuration

| Option | Default | Meaning |
| --- | --- | --- |
| `check_updates` | `true` | Enable the session-start check |
| `update_check_hours` | `24` | Hours between checks |
| `update_url` | latest release page | Override for a fork |

Environment override for tests: `CODEX_AUTO_STARTSTOP_UPDATE_URL`.

## Verification

`tests/synthetic_test.py` runs a local HTTP server that redirects like GitHub:

- A newer tag produces the notice, with the version, the update route, and the
  approval warning.
- A second session start stays silent.
- A closed port produces no output and exit code 0.

Run `python3 tests/synthetic_test.py`. Full suite: 143 checks.

## Not built

- Download, checksum, and in-place replacement. Not needed for notification.
- Release assets. The tag redirect gives the version without them.
- Launcher shim for silent updates. Rejected as a trust tradeoff.
