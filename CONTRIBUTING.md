# Contributing to myTbot

## 1. Thank you

Thank you for taking an interest in myTbot. Community review, testing, safety observations and clear technical feedback are welcome.

Emfasys Ltd currently operates myTbot on a closed-contribution model. We welcome Issues, bug reports, architectural feedback and feature suggestions, but we do not currently accept outside Pull Requests. This keeps copyright ownership clean and preserves the commercial licensing path for organisations that require terms outside AGPL v3.

## 2. Issues are welcome

Please use GitHub Issues for:

- bug reports
- feature requests
- architecture questions
- documentation feedback
- safety observations
- reproducible behaviour in paper or live mode

Do not include API keys, account IDs, broker credentials, private logs, screenshots with private account details, or any other secrets.

## 3. Pull Requests are not currently accepted

myTbot does not currently accept outside Pull Requests. To preserve clean IP ownership and maintain a clear commercial licensing path, all code changes are written and implemented internally by Emfasys Ltd.

If you have a proposed code change, please open an Issue describing the problem, context and suggested approach instead of submitting a PR.

## 4. How to report a bug

Please include:

- the myTbot version or commit
- operating system and Python version
- broker, market data feed or subsystem involved
- `APP_ENV` value, such as `paper` or `live`
- steps to reproduce
- expected behaviour
- actual behaviour
- relevant logs with all secrets removed
- whether the issue affects paper mode, live mode, or both

## 5. How to suggest a feature

Please include:

- the problem you are trying to solve
- the proposed behaviour
- why it matters
- the affected layer, such as broker, data, signal, risk, execution, API, UI or documentation
- any safety impact
- alternatives considered

## 6. How to discuss security issues

Please do not report security issues in public Issues. Follow the private reporting process in [SECURITY.md](SECURITY.md).

If you think credentials have been exposed, revoke them immediately at the broker or provider and rotate any related secrets.

## 7. Maintainer PR handling policy

If someone opens a Pull Request, maintainers should thank them, close it unmerged, and ask them to describe the idea or bug in an Issue instead.

Suggested response:

> Thank you for taking the time to prepare this contribution. As described in CONTRIBUTING.md, myTbot currently operates a closed-contribution model and we do not accept outside Pull Requests. We are closing this PR unmerged. Please open an Issue describing the bug, feature idea or proposed approach so the internal Emfasys Ltd team can review and, where appropriate, implement the change internally.
