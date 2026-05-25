# Security Policy

## Reporting vulnerabilities

Please report suspected vulnerabilities privately to Emfasys Ltd through the repository owner or the published company contact channel. Do not open a public GitHub Issue for security vulnerabilities.

Include a clear description, affected version or commit, reproduction steps where safe, and any relevant logs with secrets removed.

## Do not post secrets

Do not post any of the following in Issues, discussions, screenshots, logs or support requests:

- API keys
- broker credentials
- account IDs
- access tokens
- `.env` files
- database passwords
- logs containing secrets
- screenshots containing private account details

## API permissions

Use the minimum broker and data-provider permissions required for your deployment.

Recommended permissions:

- read-only access where trading is not required
- trade access only where needed
- no withdrawal permissions
- separate paper and live credentials where the provider supports them

Broker API keys should never include withdrawal permissions.

## Secret storage

Store secrets locally and carefully. Keep `.env` files out of version control, restrict filesystem access where possible, and avoid sharing logs or screenshots that may contain private details.

If credentials are leaked, revoke them immediately at the broker or provider, rotate related secrets and review recent account activity.
