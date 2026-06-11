# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-06-11

### Added
- **Chat Catalog System:**
  - Added `/chats` command for users to list all cataloged public and private groups, including descriptions and member counts. Private chats are marked with a lock emoji `🔐`.
  - Added `/chatadd [description]` admin command to register the current group to the catalog. If `[description]` is not provided, the bot queries the group's description from the Delta Chat core.
  - Added `/chatremove` admin command to remove the current group from the catalog.
  - Added `/private <on/off>` admin command to toggle cataloged chat privacy:
    - **Public chats:** Requests via `/chat<ID>` immediately return a group invite link.
    - **Private chats:** Requests via `/chat<ID>` require manual approval from existing members in the target group chat.
  - Added join approval and decline workflow: members reply with `/approve<ID>` to approve or `/decline<ID> [reason]` to decline. Approving sends a single-use SecureJoin invite link to the applicant, while declining notifies the applicant in a private message with an optional reason.
  - Implemented automatic securejoin link revocation to prevent reuse of invite links in private groups.
  - Integrated dynamic membership tracking (via info message hooks) to keep the catalog `member_count` database column updated in real-time.
- **Custom Welcome Messages:**
  - Added `/welcome` admin command to manage new member greeting messages (`/welcome on`, `/welcome off`, `/welcome on <custom_text>`).
  - Automatically greets new members with their chat presence count and optional custom rules text when they join the group.
- **English Localization:**
  - Standardized all bot outputs, error messages, and command responses to English.

## [1.7.0] - 2026-06-05

### Added
- **DPI Bypass Hack:** Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode:** Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [1.6.5] - 2026-06-03

### Added

- Added global `/search` feature for the bot administrator. Sending `/search <query>` to the bot in a private message now searches for matching users across all group chats where the bot is connected, grouping results and displaying which chats each contact belongs to.
- Enabled `/contact<ID>` command in private messages for the bot administrator, allowing the admin to retrieve contact vCards for users found via global search.

## [1.6.4] - 2026-06-03

### Fixed

- Added `pillow` to `requirements.txt` to resolve `No module named 'PIL'` error and enable QR code image generation for `/invite`.
- Implemented robust in-flight exception handling in `_send` to intercept "not a member of the chat" errors immediately, avoiding retry loops and transport rotations without breaking commands in active chats (reverted unstable `can_send` checks).
- Fixed attempt counting logic in `_send` failure logging to correctly report actual attempts.

## [1.6.3] - 2026-06-02

### Added

- Added `/invite` command to generate and send a SecureJoin invite link and PNG QR code image for the current group chat. Available to all users with a 10-minute cooldown (admins are exempt).

## [1.6.2] - 2026-05-20

### Added

- Upgraded `/search` command to natively support searching by domains and partial strings (e.g. `/search @testrun.org @chatmail.uk`), and configured the reply-based parser to automatically extract `@domain` handles from quoted messages.

### Changed

- Configured automatic deletion of old messages (`delete_device_after` set to 36 hours / 129600 seconds) to prevent the main SQLite database (`dc.db`) from bloating while buffering /top 24h stats.
- Disabled automatic download of message attachments and media files (`download_limit` set to 1 byte) to completely stop the `dc.db-blobs` directory from growing.

## [1.6.1] - 2026-05-20

### Added

- Enhanced `/search` command with reply-based lookups: replying to any message with `/search` will automatically extract and search for all email addresses contained in the quoted message.
- Upgraded `/search` to scan all configured transports/aliases (secondary addresses) for group contacts by parsing contact encryption info, rather than just matching against the primary address.
- Updated `/search` output to list all associated transport addresses for matched contacts.

## [1.6.0] - 2026-05-20

### Added

- Added `/search <email1> <email2> ...` command to search for group members by one or more emails simultaneously (via case-insensitive substring matching). Matches are formatted exactly like `/bounce` reports with bold names, contact links, and last-seen activity timestamps. Available to all users with a 10-minute cooldown.

## [1.5.0] - 2026-05-19

### Added

- Added complete set of in-chat transport management commands matching `tgbridge`:
  - `/addtransport <payload>` to dynamically add backup mail relays via chatmail URI or credentials.
  - `/setprimary <addr>` to switch the primary active mail relay (`configured_addr`).
- Implemented `transport_stats` SQLite database table and statistics tracking.
- Upgraded `/transports` command to show connectivity status, primary/backup labels, message counts (sent/received), and last sent/received timestamps.
- Upgraded `/rmtransport <addr>` command with full validation checks and last-transport protection.

## [1.4.0] - 2026-05-16

### Changed

- Removed automatic daily reports in all chats. Reports are now only sent upon manual request.
- Refactored background loop into a silent monitor task that only initializes tracking for new groups.
- Separated inactivity reports from activity rankings: `/bounce` now only shows inactive users, while `/top` remains for rankings.
- Increased inactivity threshold from 7 days to 14 days.

## [1.3.0] - 2026-05-11

### Added

- Added `/relays` command to find group members using regular mail providers (Yandex, Mail.ru, Rambler). Available to all users with a 10-minute cooldown.
- Added `/contact<ID>` command to share contact objects via dynamic links in reports.
- Enhanced all reports with rich formatting (bold names, square brackets for timestamps) and contact links.
- Enhanced `/relays` to automatically extract secondary addresses (transports) for all contacts by parsing encryption info.
- Added daily status reports (heartbeats) that post group statistics even if no one is inactive yet.
- Added `/top` command to show the most active members in the last 24 hours. Available to all users with a 10-minute cooldown.
- Enhanced all reports with rich formatting (bold names, square brackets for timestamps), contact links, and activity rankings.

### Changed

- Improved background loop diagnostics by logging specific errors for all RPC fallback methods.
- Fixed critical bug in `/contact<ID>` command by implementing manual vCard generation and sharing (resolved `MsgData` and `MessageViewtype` incompatibilities).
- Fixed `last_seen` extraction for contacts by properly handling `AttrDict` and dictionary fallbacks.
- Reduced log noise by changing admin fingerprint mismatch warnings to info level.

## [1.3.1] - 2026-05-12

### Changed

- Reduced inactivity threshold from 30 days to 7 days for more aggressive group management.
- Removed activity ranking from manual `/bounce` reports to keep them focused (ranking is still available via `/top`).
- Updated reports to dynamically reflect the current inactivity threshold in days.

### Fixed

- Fixed "Method not found" error in daily inactivity check by expanding RPC method fallbacks (added `get_chat_list_ids`).
- Added detailed logging for RPC method attempts to improve diagnostics.

## [1.2.0] - 2026-05-07

### Added

- Added Multi-Transport support (Backup Relays).
- Added `/transports` and `/rmtransport` commands for admin.
- Added `init transport` CLI command.
- Improved `on_start` logs to display all configured relays.

## [1.1.0] - 2026-05-07

### Added

- Added `/help` command with basic instructions and source link.
- Added `/donate` command to support development.
- Added 10-minute rate limiting (cooldown) for `/bounce` command in group chats.
- Added 30-day Grace Period for new groups to avoid "never seen" false positives.

### Changed

- Opened `/bounce` command to all users (previously admin-only).
- Admins are now exempt from the `/bounce` rate limit.
- Improved background loop robustness with multiple RPC method fallbacks.
- Added a 10-second initialization delay for background tasks.

## [1.0.0] - 2026-05-07

### Added

- Initial release of Delta Chat Bouncer Bot.
- Automated daily inactivity reports for all group chats.
- Manual inactivity check via `/bounce` command (admin-only).
- Secure admin management with `/initadmin`.
- Dockerized deployment support.
