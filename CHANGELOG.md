# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-05-07
### Added
- Added `/help` command with basic instructions and source link.
- Added `/donate` command to support development.
- Added 10-minute rate limiting (cooldown) for `/bounce` command in group chats.
### Changed
- Opened `/bounce` command to all users (previously admin-only).
- Admins are now exempt from the `/bounce` rate limit.
- Improved background loop robustness with multiple RPC method fallbacks (`get_chats`, `get_chat_ids`).
- Added a 10-second initialization delay for background tasks to allow core sync.

## [1.0.0] - 2026-05-07
### Added
- Initial release of Delta Chat Bouncer Bot.
- Automated daily inactivity reports for all group chats.
- Manual inactivity check via `/bounce` command (admin-only).
- Secure admin management with `/initadmin`.
- Dockerized deployment support.
