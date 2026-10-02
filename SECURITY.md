# Security Policy

## Supported Version

Only the current default branch is supported with security fixes.

## Reporting

Do not open a public issue for suspected credential exposure, account access, or a
vulnerability that could modify a Spotify account. Use a private repository security
advisory or contact the repository owner privately.

Include reproduction steps, affected files, expected impact, and any temporary
mitigation. Do not include real API keys, refresh tokens, user listening history, or
provider responses in the report.

## Deployment Warning

This application uses one configured Spotify account and has no per-user login.
Anyone with access to the UI can read that account and can enable automatic tool
execution. Deploy it only to trusted viewers, keep `.env` and Streamlit secrets out
of version control, and rotate credentials immediately if they are exposed.
