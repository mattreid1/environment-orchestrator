# Shared Gmail tools

Every managed Codex and Claude launcher exposes `search_mail` and `read_mail` through a host-side HTTP MCP bridge. Agents share one Gmail account. The host keeps its credential outside Git at `~/.config/environment-orchestrator/mail.json`, owned by the launcher user with mode 0600. The isolated harness and guest do not receive that file or the app password. Each session receives a temporary loopback capability, which expires when the launcher exits. Discovery does not authenticate, start a guest, or require configured mail.

## Authenticate once

Enable Google 2-Step Verification and create an app password at https://myaccount.google.com/apppasswords. Account and administrator policies can prevent app passwords. Use a Google-generated app password, never the account's normal password.

Run on the host in a private interactive terminal:

```sh
environment-mail setup --account YOUR_GMAIL_ADDRESS
environment-mail check
```

Setup prompts without echoing the password and verifies authentication and read-only access before atomically storing the credential. It accepts no password argument, environment variable, or non-interactive input. The default mailbox is `[Gmail]/All Mail`, including archived messages. Use `--mailbox INBOX` to restrict access to the inbox. If Gmail uses a localized All Mail folder name, provide that name with `--mailbox`.

No Paperclip Labs login, Google Developer Preview enrollment, or OAuth client registration is involved. Google recommends OAuth over app passwords; this initial implementation supports app passwords only. Password changes can revoke app passwords. All agents launched by this local user can access the configured mailbox. Separate company policies and multiple mail identities are not implemented.

## Agent tools

- `search_mail(query, limit=10)` accepts Gmail search expressions, returns up to 25 matching message IDs and headers in descending UID order, and reports whether more results exist. It searches only the configured mailbox.
- `read_mail(message_id)` returns headers, body text, and attachment metadata. It rejects stale mailbox identities, messages larger than 2 MiB, and malformed IDs. Body text is capped at 50,000 characters. Attachments are not exposed. The bounded message fetch can include attachment bytes internally.

IMAP uses TLS with certificate validation at the fixed endpoint `imap.gmail.com:993`. Every call opens the mailbox with EXAMINE (read-only). Header and body requests use BODY.PEEK so reading does not mark messages seen. Search expressions use IMAP literals instead of concatenated commands. There are no send, draft, label, delete, SMTP, arbitrary-host, or filesystem tools. Errors with provider-controlled text are withheld to avoid exposing credentials. Credentials are loaded for each call, so rotating them does not require restarting the orchestrator or agent.

Mail content returned to an agent enters its model conversation and reaches the configured inference service. Email is untrusted input; tool descriptions instruct agents not to treat message content as commands. The bridge does not log message bodies or provider errors.

## GitHub CLI

The NixOS deployment includes `gh` in every guest profile. Installation does not authenticate it. Host GitHub credentials are not copied to guests. Authentication and repository permissions remain a separate setup step.

## Validation

```sh
nix shell .#test-tools -c python -m unittest test_mail test_codex test_paperclip test_claude -v
```

The fixtures check TLS configuration, read-only mailbox selection, PEEK reads, literal queries, stale and oversized message rejection, private files, credential-safe errors, authenticated HTTP discovery, attachment handling, and both launchers. A successful `environment-mail check` validates real Gmail access without reading a message.
